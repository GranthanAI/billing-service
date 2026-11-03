"""Open-loop gRPC load driver for `make loadtest`: RATE CheckAndReserve calls/s for SECONDS.

1,000 synthetic Pro users, a fresh idempotency key per call, about 10% of allowed reservations
released. The entitlement cache is warmed first (GetEntitlements is not in the timed histogram);
a real deployment pays one Postgres read per user per 10 minutes.

Why not Locust: its gevent runtime added a fixed ~200 ms to every gRPC call here (min 206 ms
against a server that answers in ~3 ms), so it measured Locust, not billing-service.
"""

import asyncio
import json
import os
import random
import sys
import time
import uuid

import grpc

from billing.gen.granthan.billing.v1 import billing_pb2, billing_pb2_grpc

TARGET = os.environ.get("LOADTEST_TARGET", "localhost:50151")
META = (("authorization", "Bearer " + os.environ.get("LOADTEST_TOKEN", "loadtest-token")),)
USERS = int(os.environ.get("LOADTEST_USERS", "1000"))
NS = uuid.UUID("6f1c9c1e-3b1a-4d77-9a7e-0d6a1f0b2c11")
RATE, SECONDS = int(sys.argv[1]), int(sys.argv[2])
OUT = sys.argv[3] if len(sys.argv) > 3 else "/out/result.json"


def user(i: int) -> str:
    return str(uuid.uuid5(NS, f"loadtest-{i}"))


async def main() -> None:
    async with grpc.aio.insecure_channel(TARGET) as channel:
        stub = billing_pb2_grpc.BillingInternalStub(channel)
        sem = asyncio.Semaphore(50)

        async def warm(i: int) -> None:
            async with sem:
                await stub.GetEntitlements(
                    billing_pb2.GetEntitlementsRequest(user_id=user(i)), metadata=META, timeout=10
                )

        await asyncio.gather(*(warm(i) for i in range(USERS)))

        lat: list[float] = []
        rel_lat: list[float] = []
        errors = 0
        denied = 0
        by_code: dict[str, int] = {}

        async def one() -> None:
            nonlocal errors, denied
            uid = user(random.randrange(USERS))
            t = time.perf_counter()
            try:
                r = await stub.CheckAndReserve(
                    billing_pb2.CheckAndReserveRequest(
                        user_id=uid,
                        idempotency_key=str(uuid.uuid4()),
                        kind=billing_pb2.RESERVE_KIND_MESSAGE,
                        requested_group=random.choice(
                            [billing_pb2.MODEL_GROUP_FAST, billing_pb2.MODEL_GROUP_SMART]
                        ),
                    ),
                    metadata=META,
                    timeout=0.15,  # the contract deadline for conversation-service
                )
            except grpc.aio.AioRpcError as exc:
                errors += 1
                by_code[exc.code().name] = by_code.get(exc.code().name, 0) + 1
                return
            lat.append((time.perf_counter() - t) * 1000)
            if not r.allowed:
                denied += 1
            elif random.random() < 0.10:
                t = time.perf_counter()
                try:
                    await stub.Release(
                        billing_pb2.ReleaseRequest(
                            user_id=uid, hold_id=r.hold_id, reason="provider_error"
                        ),
                        metadata=META,
                        timeout=0.5,
                    )
                    rel_lat.append((time.perf_counter() - t) * 1000)
                except grpc.aio.AioRpcError as exc:
                    errors += 1
                    by_code["Release." + exc.code().name] = (
                        by_code.get("Release." + exc.code().name, 0) + 1
                    )

        tasks = []
        start = time.perf_counter()
        for n in range(RATE * SECONDS):
            tasks.append(asyncio.create_task(one()))
            await asyncio.sleep(max(0.0, start + (n + 1) / RATE - time.perf_counter()))
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - start

    def pct(values: list[float], p: float) -> float:
        values = sorted(values)
        return values[min(len(values) - 1, int(len(values) * p))] if values else 0.0

    result = {
        "calls": len(lat) + errors,
        "ok": len(lat),
        "errors": errors,
        "errors_by_code": by_code,
        "denied": denied,
        "releases": len(rel_lat),
        "rate": (len(lat) + errors) / elapsed,
        "client_ms": {"p50": pct(lat, 0.5), "p95": pct(lat, 0.95), "p99": pct(lat, 0.99)},
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f)
    print(json.dumps(result))


asyncio.run(main())
