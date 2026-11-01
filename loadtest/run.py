"""`make loadtest`: seed 1,000 Pro users, start billing-api, drive it, read the p99.

Needs `make up` (shared Postgres with the billing database, billing-valkey) and Docker.
Pass criteria: server-side billing_reserve_duration_seconds p99 under 10 ms (at least 99% of
calls within 10 ms) and no errors, at 200 CheckAndReserve calls/s for 2 minutes.

billing-api and the load driver both run in containers on the Docker network next to Valkey and
Postgres, which is the shape of a deployment. Driving from the Windows host adds a
port-forwarding hop of about 2 ms per Valkey call, which would measure the laptop instead.
"""

import asyncio
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DB_APP = os.environ.get(
    "BILLING_DATABASE_URL", "postgresql+asyncpg://billing_app:billing_app@localhost:5433/billing_db"
)
DB_MIGRATOR = os.environ.get(
    "BILLING_MIGRATION_DATABASE_URL",
    "postgresql+asyncpg://billing_migrator:billing_migrator@localhost:5433/billing_db",
)
VALKEY = os.environ.get("BILLING_VALKEY_URL", "redis://localhost:6380/0")
HTTP_PORT, GRPC_PORT = 8090, 50151  # 50051 is taken by another local service
USERS = 1000
RATE = int(os.environ.get("LOADTEST_RATE", "200"))  # CheckAndReserve calls per second
SECONDS = int(os.environ.get("LOADTEST_SECONDS", "120"))
NAMESPACE = uuid.UUID("6f1c9c1e-3b1a-4d77-9a7e-0d6a1f0b2c11")
P99_LIMIT_SECONDS = 0.010
API_CONTAINER = "billing-api-lt"
NETWORK = "graphgpt-net"  # the shared network: graphgpt-postgres lives on it; Valkey joins it


async def seed() -> None:
    from billing.adapters.db.engine import make_engine
    from billing.adapters.db.uow import SqlUnitOfWork
    from billing.adapters.ids import Uuid7Generator
    from billing.adapters.valkey import keys
    from billing.adapters.valkey.client import make_valkey
    from billing.app.records import OverrideRecord
    from billing.domain.calendar import ist_day
    from billing.domain.catalog import OwnerType

    engine = make_engine(DB_APP, pool_size=4)
    client = make_valkey(VALKEY)
    ids = Uuid7Generator()
    now = datetime.now(UTC)
    try:
        async with SqlUnitOfWork(engine, ids) as uow:
            pro = await uow.plans.current("pro", None)
            assert pro is not None, "run `make catalog-sync` first"
            for i in range(USERS):
                uid = uuid.uuid5(NAMESPACE, f"loadtest-{i}")
                if not await uow.overrides.active_for(OwnerType.USER, uid, now):
                    await uow.overrides.insert(
                        OverrideRecord(
                            id=uuid.uuid4(),
                            owner_type=OwnerType.USER,
                            owner_id=uid,
                            plan_version_id=pro.plan.id,
                            reason="loadtest",
                            starts_at=now - timedelta(hours=1),
                            expires_at=now + timedelta(days=30),
                            granted_by=uuid.uuid4(),
                        )
                    )
        pipe = client.pipeline()
        for i in range(USERS):  # a clean slate: today's counters and any cached entitlement
            uid = uuid.uuid5(NAMESPACE, f"loadtest-{i}")
            pipe.delete(keys.day_counters_for(uid, ist_day(now)), keys.ent(uid))
        await pipe.execute()
    finally:
        await client.aclose()
        await engine.dispose()
    print(f"seeded {USERS} Pro users")


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=check, text=True, capture_output=True)


def wait_ready(timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://localhost:{HTTP_PORT}/readyz", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise SystemExit("billing-api did not become ready")


def histogram(text: str) -> tuple[list[tuple[float, float]], float]:
    """Cumulative (le, count) pairs for kind=message, and the total count."""
    pattern = re.compile(
        r'billing_reserve_duration_seconds_bucket\{kind="message",le="([^"]+)"\} ([0-9.e+]+)'
    )
    buckets = sorted(
        (float("inf") if le == "+Inf" else float(le), float(count))
        for le, count in pattern.findall(text)
    )
    return buckets, buckets[-1][1] if buckets else 0.0


def quantile(buckets: list[tuple[float, float]], total: float, q: float) -> float:
    """Linear interpolation inside the bucket that holds the quantile (Prometheus style)."""
    rank, prev_le, prev_count = q * total, 0.0, 0.0
    for le, count in buckets:
        if count >= rank:
            if le == float("inf"):
                return prev_le
            return prev_le + (le - prev_le) * ((rank - prev_count) / max(count - prev_count, 1e-9))
        prev_le, prev_count = le, count
    return prev_le


def cpu_name() -> str:
    try:
        out = subprocess.run(
            ["wmic", "cpu", "get", "name"], capture_output=True, text=True, timeout=10
        ).stdout.split("\n")
        return next((line.strip() for line in out[1:] if line.strip()), platform.processor())
    except Exception:
        return platform.processor()


def main() -> int:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        cwd=ROOT,
        env={**env, "BILLING_MIGRATION_DATABASE_URL": DB_MIGRATOR},
    )
    asyncio.run(seed())

    print("building images ...", flush=True)
    docker("build", "-q", "-t", "billing-service:loadtest", str(ROOT))
    docker(
        "build",
        "-q",
        "-t",
        "billing-loadtest-driver",
        "-f",
        str(ROOT / "loadtest" / "Dockerfile"),
        str(ROOT),
    )
    docker("network", "connect", NETWORK, "billing-valkey", check=False)  # fine if already joined
    docker("rm", "-f", API_CONTAINER, check=False)
    docker(
        "run",
        "-d",
        "--name",
        API_CONTAINER,
        "--network",
        NETWORK,
        "-p",
        f"{HTTP_PORT}:8080",
        "-p",
        f"{GRPC_PORT}:{GRPC_PORT}",
        "-e",
        "BILLING_DATABASE_URL=postgresql+asyncpg://billing_app:billing_app@"
        "graphgpt-postgres:5432/billing_db",
        "-e",
        "BILLING_MIGRATION_DATABASE_URL=unused",
        "-e",
        "BILLING_VALKEY_URL=redis://billing-valkey:6379/0",
        "-e",
        "KAFKA_BOOTSTRAP_SERVERS=unused",
        "-e",
        "RAZORPAY_KEY_ID=k",
        "-e",
        "RAZORPAY_KEY_SECRET=s",
        "-e",
        "RAZORPAY_WEBHOOK_SECRET=w",
        "-e",
        "SERVICE_TOKEN_KEYS=loadtest-token",
        "-e",
        "SUPPLIER_STATE_CODE=10",
        "-e",
        f"GRPC_PORT={GRPC_PORT}",
        "billing-service:loadtest",
    )
    out_dir = Path(tempfile.mkdtemp(prefix="billing-loadtest-"))
    try:
        wait_ready()
        driver = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                NETWORK,
                "-v",
                f"{out_dir}:/out",
                "-e",
                f"LOADTEST_TARGET={API_CONTAINER}:{GRPC_PORT}",
                "-e",
                "LOADTEST_TOKEN=loadtest-token",
                "-e",
                f"LOADTEST_USERS={USERS}",
                "billing-loadtest-driver",
                "python",
                "/app/drive.py",
                str(RATE),
                str(SECONDS),
                "/out/result.json",
            ]
        )
        with urllib.request.urlopen(f"http://localhost:{HTTP_PORT}/metrics") as r:
            metrics = r.read().decode()
    finally:
        docker("rm", "-f", API_CONTAINER, check=False)

    buckets, total = histogram(metrics)
    p50, p95, p99 = (quantile(buckets, total, q) for q in (0.50, 0.95, 0.99))
    within = next((c for le, c in buckets if le == P99_LIMIT_SECONDS), 0.0)
    result = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
    c = result["client_ms"]

    print(
        f"\nmachine: {cpu_name()} ({os.cpu_count()} logical CPUs), {platform.platform()}, "
        f"Docker Desktop containers on the same network"
    )
    print(
        f"calls: {result['calls']} at {result['rate']:.0f}/s  denied: {result['denied']}  "
        f"releases: {result['releases']}  errors: {result['errors']}"
    )
    print(
        f"server-side billing_reserve_duration_seconds ({int(total)} calls): "
        f"p50={p50 * 1000:.2f} ms  p95={p95 * 1000:.2f} ms  p99={p99 * 1000:.2f} ms  "
        f"({within / total:.2%} within 10 ms)"
    )
    print(
        f"client-side (gRPC + Docker network): "
        f"p50={c['p50']:.1f} ms  p95={c['p95']:.1f} ms  p99={c['p99']:.1f} ms"
    )
    ok = total > 0 and within / total >= 0.99 and result["errors"] == 0
    passed = ok and driver.returncode == 0
    print("RESULT:", "PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
