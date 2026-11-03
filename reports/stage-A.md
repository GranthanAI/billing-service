# Stage A: stabilise Phases 5 and 6

Status: **stopped at the load gate.** Everything else in Stage A is done. Not pushed.

## Done
- **Full suite:** 330 passed, domain coverage 99.5% (one full run after the fixes; the exit check
  asks for two in a row, which waits on the load gate).
- **`test_expired_hold_is_swept_and_late_usage_counts_again`:** the cause was not Prometheus.
  The sweeper expires every due hold in the shared session Valkey, so holds left by earlier tests
  were swept and counted too (27 instead of 1). The test now clears `holds:due` first. Every other
  test that reads a metric (`WEBHOOK_REJECTED`, `WEBHOOK_FAILED`, `JOBS_DEAD`, `HOLDS_EXPIRED`)
  already asserts a delta.
- **Entitlement freshness:** `ValkeyEntitlementCache.delete` now also publishes
  `billing:ent:invalidate <user_id>`. Each API process runs `EntitlementService.listen()` (started
  in the lifespan), which drops that user's L1 entry and clears L1 whenever the subscription is
  (re)established. Reconnect backoff 0.5 s to 10 s, logged. The 5 s L1 TTL stays as backstop.
  Test: two services on one Valkey, invalidation seen in under 100 ms.
- **Migrations:** `alembic upgrade` from empty to 0011, 0011 to head, and a downgrade to 0008 and
  back, all on a scratch database.
- **Accepted deviation:** downgrade and resume return 201 with Checkout options (a new mandate
  needs authorising); the LLD says 200 for a downgrade.

## Load gate: NOT met
Target: p99 < 10 ms, 0 errors, 200 reserves/s for 2 minutes.

| Run | Server p50 / p95 / p99 | Errors |
| --- | --- | --- |
| Phase 6 head, before changes | 3.8 / 20.4 / 24.5 ms | 1,879 to 3,002 |
| Phase 6 head, loop-lag + diagnostics | 3.6 / 20.6 / 24.8 ms | 3,585 |
| uvloop + API on CPUs 0-3, driver on 4-7 | 4.3 / 21.4 / 25.4 ms | 5,301 |
| **Phase 5 commit `aee436f` (no Phase 6 code)** | 4.1 / 22.3 / 32.3 ms | 6,662 |

The last row is the finding: the Phase 5 commit fails the same way on this machine today, so the
failure is environmental, not a Phase 6 regression. Machine: AMD64 Family 23 Model 104 (12 logical
CPUs), Windows 11, Docker Desktop (7.4 GiB), already at 30-55% CPU before the test from unrelated
containers (Cassandra, Milvus, Kafka, Neo4j) and other agents' work.

Error classification: every error is gRPC `DEADLINE_EXCEEDED` (the driver's 150 ms deadline), all
client-observed. The server's logged tracebacks are `execute_batch` failures from replying after
the client gave up.

Checklist:
1. Valkey pool: client built with `from_url` (no cap); not the limit. Valkey RTT inside its container
   0.5 ms avg, 3 ms max; the EVALSHA script costs ~0.15 ms.
2. gRPC `maximum_concurrent_rpcs`: unset (unbounded); no thread pool in use (aio server).
3. Hot-path logging: none in the reserve path; the log lines seen are the gRPC tracebacks above.
4. Label cardinality: `kind, decision, reason, plan` is small and bounded.
5. L1 hit rate: after warm-up the reserve issues one EVALSHA and no GET (1,000 warm-up GETs only).
6. Postgres on the hot path: **0 statements after warm-up** (counted in-process, 3,000 reserves).
7. uvloop: was **not** enabled; added `uvloop` for Linux (`uvicorn --loop auto` picks it up).
8. Event-loop lag: p50 0.9 to 6.8 ms, p95 9 to 25 ms, p99 22 to 47 ms while the API container used
   ~2% CPU: the loop is being starved, not busy.
9. Driver vs server CPU: now pinned to separate cores (`--cpuset-cpus`); no improvement.

Remaining bottleneck: scheduling jitter in the Docker Desktop VM on a loaded host. A sequential
in-process run (no Docker) gives p50 2.7 to 4.5 ms with one Valkey ping costing 1.6 to 1.9 ms
through the host port forward, so the reserve itself is one round trip plus ~1 ms of Python.

**Needed to close the gate:** run `make loadtest` on a quiet machine (stop unrelated containers).
I did not lower the bar.

## Changes
`loadtest/run.py` and `drive.py` (errors by code, loop-lag quantiles, log tail, CPU pinning),
`pyproject.toml`/`uv.lock` (uvloop), `adapters/valkey/entitlement_cache.py`, `keys.py`,
`app/entitlements.py`, `app/ports.py`, `main_api.py`, `tests/integration/test_hot_path.py`.

## For later stages
Nothing blocks Stage B in code. B onward assumes the load gate still holds; re-check it there.
