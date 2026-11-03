"""Idempotency-Key middleware (LLD "Idempotency middleware"), for every POST but the webhook.

1. hash method + path + body; 2. IdempotencyRepo.begin; 3. replay / mismatch / in progress;
4. run the handler and store the response. A 5xx is not stored: the key is released instead.
"""

import hashlib
from collections.abc import Callable
from typing import Any
from uuid import UUID

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from billing.api.rest.errors import problem
from billing.app.errors import IdempotencyKeyReused, RequestInProgress, Unauthenticated
from billing.app.ports import UnitOfWork
from billing.app.records import InProgress, Mismatch, Replay

PREFIX = "/billing/v1/"
WEBHOOK_PATH = "/billing/v1/webhooks/razorpay"
MAX_BODY = 1_000_000
LOCK_SECONDS = 30


def parse_user_id(raw: str | None) -> UUID | None:
    try:
        return UUID(raw) if raw else None
    except ValueError:
        return None


class IdempotencyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or not path.startswith(PREFIX)
            or path == WEBHOOK_PATH
        ):
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        user = parse_user_id(headers.get("x-user-id"))
        if user is None:
            await self._send(send, problem("UNAUTHENTICATED", Unauthenticated.title, ""))
            return
        key_raw = headers.get("idempotency-key")
        key = parse_user_id(key_raw)
        if key is None:
            await self._send(
                send,
                problem(
                    "VALIDATION_FAILED",
                    "The request is not valid",
                    "Idempotency-Key header (a UUID) is required",
                    400,
                ),
            )
            return
        body = await self._read_body(receive)
        if body is None:
            await self._send(
                send, problem("VALIDATION_FAILED", "The request is too large", "", 413)
            )
            return

        request_hash = hashlib.sha256(
            scope["method"].encode() + b"\n" + path.encode() + b"\n" + body
        ).hexdigest()
        uow_factory: Callable[[], UnitOfWork] = scope["app"].state.uow_factory
        scope_name, key_text = str(user), str(key)
        async with uow_factory() as uow:
            begun = await uow.idempotency.begin(scope_name, key_text, request_hash, LOCK_SECONDS)
        if isinstance(begun, Mismatch):
            await self._send(send, _as_response(IdempotencyKeyReused()))
            return
        if isinstance(begun, InProgress):
            await self._send(send, _as_response(RequestInProgress()))
            return
        if isinstance(begun, Replay):
            stored: dict[str, Any] = begun.body
            await send(
                {
                    "type": "http.response.start",
                    "status": begun.status,
                    "headers": [
                        (b"content-type", stored["content_type"].encode()),
                        (b"idempotent-replayed", b"true"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": stored["body"].encode()})
            return

        sent_body = False

        async def replay_receive() -> Message:
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        start: Message = {}
        chunks: list[bytes] = []

        async def capture(message: Message) -> None:
            if message["type"] == "http.response.start":
                start.update(message)
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        try:
            await self.app(scope, replay_receive, capture)
        except BaseException:
            await self._release(uow_factory, scope_name, key_text)
            raise
        status = int(start["status"])
        response_body = b"".join(chunks)
        if status >= 500:
            await self._release(uow_factory, scope_name, key_text)
        else:
            content_type = next(
                (v.decode() for k, v in start["headers"] if k.lower() == b"content-type"),
                "application/json",
            )
            async with uow_factory() as uow:
                await uow.idempotency.complete(
                    scope_name,
                    key_text,
                    status,
                    {"body": response_body.decode(), "content_type": content_type},
                )
        await send(start)
        await send({"type": "http.response.body", "body": response_body})

    @staticmethod
    async def _release(uow_factory: Callable[[], UnitOfWork], scope_name: str, key: str) -> None:
        async with uow_factory() as uow:
            await uow.idempotency.release(scope_name, key)

    @staticmethod
    async def _read_body(receive: Receive) -> bytes | None:
        body: bytes = b""
        while True:
            message = await receive()
            body += bytes(message.get("body", b""))
            if len(body) > MAX_BODY:
                return None
            if not message.get("more_body", False):
                return body

    @staticmethod
    async def _send(send: Send, response: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": response.status_code,
                "headers": response.raw_headers,
            }
        )
        await send({"type": "http.response.body", "body": response.body})


def _as_response(exc: Any) -> Any:
    return problem(exc.code, exc.title, exc.detail)
