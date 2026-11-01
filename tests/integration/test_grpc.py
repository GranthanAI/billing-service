from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import grpc
import pytest
import pytest_asyncio
import valkey.asyncio as valkey_asyncio
from grpc_health.v1 import health_pb2, health_pb2_grpc

from billing.api.grpc.internal import BillingInternalService, start_grpc_server
from billing.gen.granthan.billing.v1 import billing_pb2, billing_pb2_grpc
from tests.integration.conftest import FreshDb
from tests.integration.hot_support import Stack, make_stack, make_user

TOKEN_A, TOKEN_B = "token-a", "token-b"


def auth(token: str = TOKEN_A) -> list[tuple[str, str]]:
    return [("authorization", f"Bearer {token}"), ("x-correlation-id", "c-grpc-test")]


@pytest_asyncio.fixture
async def server(hot: FreshDb, vk: valkey_asyncio.Valkey) -> AsyncIterator[tuple[Stack, str]]:
    stack = make_stack(hot, vk)
    srv, port = await start_grpc_server(stack.reserve, [TOKEN_A, TOKEN_B], 0, production=False)
    yield stack, f"localhost:{port}"
    await srv.stop(0)


async def _call(target: str, fn: str, request: Any, **kw: Any) -> Any:
    async with grpc.aio.insecure_channel(target) as channel:
        stub = billing_pb2_grpc.BillingInternalStub(channel)
        return await getattr(stub, fn)(request, **kw)


def reserve_request(uid: str, key: str | None = None) -> billing_pb2.CheckAndReserveRequest:
    return billing_pb2.CheckAndReserveRequest(
        user_id=uid,
        idempotency_key=key or str(uuid4()),
        kind=billing_pb2.RESERVE_KIND_MESSAGE,
        requested_group=billing_pb2.MODEL_GROUP_SMART,
    )


async def test_missing_or_bad_token_is_unauthenticated_and_rotation_works(
    server: tuple[Stack, str], hot: FreshDb
) -> None:
    _, target = server
    uid = str(await make_user(hot, "pro"))
    for metadata in (None, auth("wrong"), [("authorization", "Basic abc")]):
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await _call(target, "CheckAndReserve", reserve_request(uid), metadata=metadata)
        assert exc.value.code() is grpc.StatusCode.UNAUTHENTICATED
    for token in (TOKEN_A, TOKEN_B):  # both keys in SERVICE_TOKEN_KEYS are accepted
        r = await _call(target, "CheckAndReserve", reserve_request(uid), metadata=auth(token))
        assert r.allowed


async def test_response_matches_the_proto_contract(server: tuple[Stack, str], hot: FreshDb) -> None:
    _, target = server
    uid = str(await make_user(hot, "pro"))
    r = await _call(target, "CheckAndReserve", reserve_request(uid), metadata=auth())
    assert r.allowed and r.hold_id
    assert r.deny_reason == billing_pb2.DENY_REASON_UNSPECIFIED
    assert r.granted_group == billing_pb2.MODEL_GROUP_SMART and not r.downgraded
    assert r.max_context_tokens == 64000 and r.remaining_today == 59
    assert r.funded_by == "plan" and r.message_key == ""
    assert r.billing_account_id == ""  # a plan override, no billing account yet
    assert abs(r.hold_expires_at_unix - (datetime.now(UTC).timestamp() + 600)) < 30

    released = await _call(
        target,
        "Release",
        billing_pb2.ReleaseRequest(user_id=uid, hold_id=r.hold_id, reason="provider_error"),
        metadata=auth(),
    )
    assert released.released
    ent = await _call(
        target, "GetEntitlements", billing_pb2.GetEntitlementsRequest(user_id=uid), metadata=auth()
    )
    assert (ent.plan_code, ent.plan_version, ent.source) == ("pro", 1, "override")
    assert len(ent.limits) == 12 and ent.limits["messages_per_day"] == 60
    assert "claude-haiku-5-5" in ent.allowed_models and ent.version_hash

    denied = await _call(
        target,
        "CheckAndReserve",
        billing_pb2.CheckAndReserveRequest(
            user_id=uid,
            idempotency_key="x",
            kind=billing_pb2.RESERVE_KIND_MESSAGE,
            requested_group=billing_pb2.MODEL_GROUP_BEST,
        ),
        metadata=auth(),
    )
    assert not denied.allowed
    assert denied.deny_reason == billing_pb2.DENY_REASON_MODEL_NOT_IN_PLAN
    assert denied.message_key == "plan.model_not_included"


async def test_bad_requests_are_invalid_argument(server: tuple[Stack, str]) -> None:
    _, target = server
    bad = [
        billing_pb2.CheckAndReserveRequest(
            user_id="not-a-uuid", idempotency_key="k", kind=billing_pb2.RESERVE_KIND_MESSAGE
        ),
        billing_pb2.CheckAndReserveRequest(user_id=str(uuid4()), idempotency_key="k"),
        billing_pb2.CheckAndReserveRequest(
            user_id=str(uuid4()), kind=billing_pb2.RESERVE_KIND_MESSAGE
        ),
        billing_pb2.CheckAndReserveRequest(
            user_id=str(uuid4()), idempotency_key="k", kind=billing_pb2.RESERVE_KIND_MESSAGE
        ),  # no group
    ]
    for request in bad:
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await _call(target, "CheckAndReserve", request, metadata=auth())
        assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT


async def test_health_service_reports_serving(server: tuple[Stack, str]) -> None:
    _, target = server
    async with grpc.aio.insecure_channel(target) as channel:
        stub = health_pb2_grpc.HealthStub(channel)
        for name in ("", "granthan.billing.v1.BillingInternal"):
            r = await stub.Check(health_pb2.HealthCheckRequest(service=name))
            assert r.status == health_pb2.HealthCheckResponse.SERVING


class FakeContext:
    """Just enough ServicerContext to see what the guard does."""

    def __init__(self, remaining: float | None) -> None:
        self._remaining = remaining
        self.aborted: grpc.StatusCode | None = None

    def invocation_metadata(self) -> list[tuple[str, str]]:
        return auth()

    def time_remaining(self) -> float | None:
        return self._remaining

    async def abort(self, code: grpc.StatusCode, details: str) -> None:
        self.aborted = code
        raise RuntimeError("aborted")


async def test_a_tiny_deadline_is_refused_without_touching_valkey(
    server: tuple[Stack, str], hot: FreshDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack, target = server
    touched: list[str] = []
    real = stack.counters.reserve

    async def spy(args: Any) -> Any:
        touched.append(args.hold_id)
        return await real(args)

    monkeypatch.setattr(stack.counters, "reserve", spy)
    service = BillingInternalService(stack.reserve, [TOKEN_A])
    uid = str(await make_user(hot, "pro"))

    ctx = FakeContext(remaining=0.002)  # under 5 ms left
    with pytest.raises(RuntimeError, match="aborted"):
        await service.CheckAndReserve(reserve_request(uid), ctx)  # type: ignore[arg-type]
    assert ctx.aborted is grpc.StatusCode.DEADLINE_EXCEEDED and touched == []

    ok = FakeContext(remaining=0.2)
    assert (await service.CheckAndReserve(reserve_request(uid), ok)).allowed  # type: ignore[arg-type]
    assert len(touched) == 1

    with pytest.raises(grpc.aio.AioRpcError) as exc:  # and over the wire
        await _call(target, "CheckAndReserve", reserve_request(uid), metadata=auth(), timeout=0.001)
    assert exc.value.code() is grpc.StatusCode.DEADLINE_EXCEEDED
