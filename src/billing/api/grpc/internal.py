"""The BillingInternal gRPC service: CheckAndReserve, Release, GetEntitlements.

Auth: metadata `authorization: Bearer <token>` against SERVICE_TOKEN_KEYS (comma-separated, so
tokens can rotate). The user id comes from the verified JWT at api-gateway; billing never
trusts one from a client payload.
"""

import hmac
from dataclasses import asdict
from typing import Any
from uuid import UUID

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from grpc_reflection.v1alpha import reflection

from billing.app.context import correlation_id_var
from billing.app.reserve import CheckAndReserveRequest, ReserveService
from billing.domain.catalog import DenyReason, ModelGroup, ReserveKind
from billing.gen.granthan.billing.v1 import billing_pb2, billing_pb2_grpc

Ctx = grpc.aio.ServicerContext[Any, Any]
MIN_REMAINING_SECONDS = 0.005  # under 5 ms left: not worth touching Valkey

_GROUP_FROM_PROTO = {
    billing_pb2.MODEL_GROUP_FAST: ModelGroup.FAST,
    billing_pb2.MODEL_GROUP_SMART: ModelGroup.SMART,
    billing_pb2.MODEL_GROUP_BEST: ModelGroup.BEST,
    billing_pb2.MODEL_GROUP_ULTRA: ModelGroup.ULTRA,
}
_KIND_FROM_PROTO = {
    billing_pb2.RESERVE_KIND_MESSAGE: ReserveKind.MESSAGE,
    billing_pb2.RESERVE_KIND_YOUTUBE_VIDEO: ReserveKind.YOUTUBE_VIDEO,
    billing_pb2.RESERVE_KIND_MANIM_VIDEO: ReserveKind.MANIM_VIDEO,
}


def _group_to_proto(group: ModelGroup | None) -> billing_pb2.ModelGroup.ValueType:
    if group is None:
        return billing_pb2.MODEL_GROUP_UNSPECIFIED
    return billing_pb2.ModelGroup.Value(f"MODEL_GROUP_{group.name}")


def _deny_to_proto(reason: DenyReason | None) -> billing_pb2.DenyReason.ValueType:
    if reason is None:
        return billing_pb2.DENY_REASON_UNSPECIFIED
    return billing_pb2.DenyReason.Value(f"DENY_REASON_{reason.name}")


class BillingInternalService(billing_pb2_grpc.BillingInternalServicer):
    def __init__(self, reserve: ReserveService, tokens: list[str]) -> None:
        self._reserve = reserve
        self._tokens = [t.encode() for t in tokens if t]

    async def _guard(self, context: Ctx) -> None:
        meta = {k: v for k, v in context.invocation_metadata() or ()}
        presented = str(meta.get("authorization", "")).removeprefix("Bearer ").strip().encode()
        # compare against every configured token so timing does not reveal which one matched
        ok = False
        for token in self._tokens:
            ok |= hmac.compare_digest(presented, token)
        if not ok:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing or invalid service token")
        remaining = context.time_remaining()
        if remaining is not None and remaining < MIN_REMAINING_SECONDS:
            await context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "deadline too close")
        correlation_id_var.set(str(meta.get("x-correlation-id", "")))

    @staticmethod
    async def _uuid(value: str, context: Ctx) -> UUID:
        try:
            return UUID(value)
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"invalid user_id {value!r}")
            raise  # unreachable: abort raises

    async def CheckAndReserve(  # noqa: N802
        self, request: billing_pb2.CheckAndReserveRequest, context: Ctx
    ) -> billing_pb2.CheckAndReserveResponse:
        await self._guard(context)
        kind = _KIND_FROM_PROTO.get(request.kind)
        if kind is None:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "kind is required")
        if not request.idempotency_key:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "idempotency_key is required")
        user_id = await self._uuid(request.user_id, context)
        try:
            result = await self._reserve.check_and_reserve(
                CheckAndReserveRequest(
                    user_id=user_id,
                    idempotency_key=request.idempotency_key,
                    kind=kind,
                    requested_group=_GROUP_FROM_PROTO.get(request.requested_group),
                    requested_model=request.requested_model,
                )
            )
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        return billing_pb2.CheckAndReserveResponse(
            allowed=result.allowed,
            deny_reason=_deny_to_proto(result.deny_reason),
            hold_id=result.hold_id,
            granted_group=_group_to_proto(result.granted_group),
            downgraded=result.downgraded,
            max_context_tokens=result.max_context_tokens,
            billing_account_id=str(result.billing_account_id) if result.billing_account_id else "",
            message_key=result.message_key,
            hold_expires_at_unix=result.hold_expires_at_unix,
            remaining_today=result.remaining_today,
            funded_by=result.funded_by,
            granted_model=result.granted_model,
        )

    async def Release(  # noqa: N802
        self, request: billing_pb2.ReleaseRequest, context: Ctx
    ) -> billing_pb2.ReleaseResponse:
        await self._guard(context)
        user_id = await self._uuid(request.user_id, context)
        released = await self._reserve.release(user_id, request.hold_id, request.reason)
        return billing_pb2.ReleaseResponse(released=released)

    async def GetEntitlements(  # noqa: N802
        self, request: billing_pb2.GetEntitlementsRequest, context: Ctx
    ) -> billing_pb2.Entitlements:
        await self._guard(context)
        user_id = await self._uuid(request.user_id, context)
        ent = await self._reserve.get_entitlements(user_id)
        pv = ent.grant.plan_version
        return billing_pb2.Entitlements(
            user_id=str(user_id),
            billing_account_id=str(ent.grant.billing_account_id)
            if ent.grant.billing_account_id
            else "",
            source=ent.grant.source.value,
            plan_code=pv.plan_code,
            plan_version=pv.version,
            allowed_models=await self._reserve.allowed_models(ent),
            limits=asdict(ent.limits),
            version_hash=ent.version_hash,
        )


async def start_grpc_server(
    reserve: ReserveService,
    tokens: list[str],
    port: int,
    *,
    production: bool,
) -> tuple[grpc.aio.Server, int]:
    """Start the server on the running loop. Returns it and the bound port (port 0 picks one)."""
    server = grpc.aio.server()
    billing_pb2_grpc.add_BillingInternalServicer_to_server(
        BillingInternalService(reserve, tokens), server
    )
    health_servicer = health.aio.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    service_names = [
        billing_pb2.DESCRIPTOR.services_by_name["BillingInternal"].full_name,
        health_pb2.DESCRIPTOR.services_by_name["Health"].full_name,
    ]
    if not production:
        reflection.enable_server_reflection((*service_names, reflection.SERVICE_NAME), server)
    bound = server.add_insecure_port(f"[::]:{port}")
    await server.start()
    for name in ("", service_names[0]):
        await health_servicer.set(name, health_pb2.HealthCheckResponse.SERVING)
    return server, bound
