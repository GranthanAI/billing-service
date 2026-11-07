"""StorageUsage over HTTP, and the GiB arithmetic of storage.bill."""

from uuid import uuid4

import httpx
import pytest

from billing.adapters.storage_usage import HttpStorageUsage, StorageUsageUnavailable
from billing.jobs.handlers.wallet import GIB, over_gb_of

USER = uuid4()


def client(handler: httpx.MockTransport, sleeps: list[float]) -> HttpStorageUsage:
    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return HttpStorageUsage("http://files", "tok", transport=handler, sleep=sleep)


def test_over_gb_is_whole_gib_above_ten_rounded_up() -> None:
    assert over_gb_of(0) == 0
    assert over_gb_of(10 * GIB - 1) == 0
    assert over_gb_of(10 * GIB) == 0  # exactly 10 GiB is free
    assert over_gb_of(10 * GIB + 1) == 1
    assert over_gb_of(11 * GIB) == 1
    assert over_gb_of(11 * GIB + 1) == 2
    assert over_gb_of(int(25.5 * GIB)) == 16
    assert GIB == 1_073_741_824


async def test_request_shape_and_parse() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"user_id": str(USER), "bytes_used": 12345, "measured_at": "2026-11-01T04:30:00Z"},
        )

    assert await client(httpx.MockTransport(handle), []).bytes_used(USER) == 12345
    (request,) = seen
    assert request.url.path == "/internal/storage/usage"
    assert request.url.params["user_id"] == str(USER)
    assert request.headers["authorization"] == "Bearer tok"


async def test_server_errors_are_retried_three_times_then_fail() -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    with pytest.raises(StorageUsageUnavailable):
        await client(httpx.MockTransport(handle), sleeps).bytes_used(USER)
    assert len(calls) == 4  # the first try and 3 retries
    assert sleeps == [0.2, 0.4, 0.8]


async def test_a_transient_failure_then_success() -> None:
    answers = iter([httpx.Response(429), httpx.Response(200, json={"bytes_used": 7})])

    def handle(request: httpx.Request) -> httpx.Response:
        return next(answers)

    assert await client(httpx.MockTransport(handle), []).bytes_used(USER) == 7


async def test_timeouts_are_retried() -> None:
    calls: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"bytes_used": 99})

    assert await client(httpx.MockTransport(handle), []).bytes_used(USER) == 99
    assert len(calls) == 3


async def test_other_client_errors_fail_at_once() -> None:
    calls: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401)

    with pytest.raises(StorageUsageUnavailable):
        await client(httpx.MockTransport(handle), []).bytes_used(USER)
    assert len(calls) == 1
