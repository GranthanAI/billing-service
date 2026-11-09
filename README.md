# billing-service

Billing service for GranthanAI: plans, subscriptions, payments, invoices, credits and entitlements. FastAPI (HTTP) and gRPC, PostgreSQL 17, Valkey, and JSON events on Kafka. Start with `PHASES.md` (build plan), `docs/hld.md` and `docs/lld.md` (design), `runbooks/` (operations, including `runbooks/email.md`) and `contracts/` (API, event and proto contracts).

## Testing

| Command | What it runs |
|---|---|
| `make test-fast` | unit + property + health. No Docker, no coverage, about 40 s. Run after every change. |
| `make test-int` | integration tests, `-m "not slow"`, xdist. Reuses the services of `docker-compose.test.yml` started by `make test-up`. Run before each commit. |
| `make test-file FILE=tests/integration/test_x.py` | one file against the running services. |
| `make test-all` | everything, with the 95% coverage gate on `billing.domain`. Run once per phase, at the end. |
| `make test-full-bg` | `test-all` in the background; writes `reports/test-run-latest.txt` and returns immediately. |
| `make test-up` / `make test-down` | start / stop (and wipe) the test services. |

**Rule.** While working run `test-fast` plus `test-file` for the files you touched. Run `test-int` before each commit. Run `test-all` once per phase, at the end.

**One integration run at a time.** Two runs share the Docker services and cause connection resets and confusing failures.

Details:

- `TEST_WORKERS`: xdist workers for the integration tiers. Default 3 on Windows, 4 elsewhere (Docker Desktop drops connections under higher load).
- `TEST_RUN_ID` / `RUN_ID`: a short random id per run, included in database names. Valkey uses a per-run-and-worker database index. Leftover test databases older than an hour are dropped at session start.
- `TEST_PG_URL`, `TEST_VALKEY_URL`, `TEST_VALKEY_LRU_URL`, `TEST_KAFKA_BOOTSTRAP` are set by the make targets. Without them the tests fall back to Testcontainers.
- The `serial` xdist group (`--dist loadgroup`): Kafka tests run together in one worker.
- Markers: `slow`, `kafka`, `sim`. pytest-timeout is 60 s per test.
