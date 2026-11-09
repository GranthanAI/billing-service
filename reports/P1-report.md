# P1: billing-service, report

Plan: `docs/Integrations/GranthanAI_Billing_Integration_Plan.md`, phase P1. Base commit `7eef6c1`.

## What shipped

| Commit | Stage |
| --- | --- |
| `2c9465b` | P1a: payment simulator core (settings, migration 0019, mode guard, `SimulatorGateway`) |
| `8e3cbd9` | P1b: sim jobs, webhook delivery with chaos, parity test |
| `6b61046` | P1c: simulated checkout page, control API, `provider`/`simulator_url`, `/me` fields, SI/SB/SC, watermark |
| `47733a2` | P1d: `entitlements.changed`, `usage.metered`, attempt-aware ids, unregistered-GST wallet fix |
| (this) | P1e: docs, demo script, lifecycle-on-simulator tests, this report |

## Tests

- Unit and property: 354 passed, domain coverage 96.4% (gate 95%).
- Integration: 366 passed in the last full run, plus a rerun of the 3 that failed there: `test_ops` (a grant list needed `gateway_mode`, fixed) and two timeouts under load (`test_200_concurrent_reserves...`, `test_concurrent_claimers...`), which pass when rerun alone.
- New: `test_sim_gateway` (every gateway method), `test_sim_guards`, `test_sim_jobs` (renewal success, fail then halt, halt, pay-now, duplicates plus reorder give the same end state, dropped webhooks recovered by `checkout.reconcile` and `subscription.poll`), `test_sim_checkout` (full HTTP flow, 403 for tampered or expired tokens, 404 when simulation is off), `test_p1d_contracts`, `test_lifecycle_sim` (13 lifecycle tests unchanged against the simulator), `tests/unit/test_sim_parity.py` (key tree equal to every fixture), `test_sim_invoice`.
- Not re-run as one final `make test-all` after the last two small test edits (`test_ops` grant list); those files were run alone and pass.

## Exit check (real run)

Throwaway Postgres 17, Valkey and Kafka; API plus worker with `PAYMENT_SIMULATION=true`; `scripts/sim_checkout_demo.sh` over HTTP:

```
1. a new user is on the Free plan   (payment_mode: simulated)
2. start the checkout               (simulator_url: /api/v1/billing/sim/checkout/sub_SIM28H5ONnYaWw?t=...)
4. pay with the test card 4111111111111111 and OTP 123456
5. verify  {"status":"active"}
6. plan: pro, status: active
7. invoice: SI/2627/000001
OK: Free -> pro with invoice SI/2627/000001 (simulated payment, no real money)
```

Afterwards: 3 simulator webhooks `delivered`, 1 payment with `gateway = simulator`, `gateway_mode = simulator`.

## Deviations and things to know

1. The plan file is in `docs/Integrations/`, not the repo root (untracked; I did not commit it).
2. `payments.gateway` did not exist; migration 0019 adds it (plus `billing.gateway_mode`). `GatewayPayment.gateway` and `PaymentRecord.gateway` are new fields with a default of `razorpay`.
3. `/me` `limits` now uses `-1` for unlimited (plan 4.7); it was `null`. One existing test changed. Decide if any consumer needs the old form.
4. `entitlements.changed` for an organisation change is enqueued as a job (`entitlements.fanout`) in the same transaction, which then writes one event per member, because org-service must not be called inside a transaction. User changes write the event directly. Catalog `apply-limits` announces the moved subscribers only (Free users have no row to enumerate). `version_hash` is an opaque per-event marker, not a hash of the entitlement.
5. Payment-link signature has no `verify_*` method on the port; the simulator has its own `verify_payment_link_signature` (Razorpay's `link|reference|status|payment`).
6. Halted by the simulator ends the billing subscription (that is billing's existing rule), so "halt" tests expect `ended`.
7. Lifecycle on the simulator: 8 tests run at `SIM_MONTH_SECONDS=60`; 5 run with calendar months because they depend on a 10-minute downgrade lead, 24 to 48 hour reminder windows or exact dates; the 40-day timeline test is skipped (it relies on the fake's `10days` period).
8. Existing tests edited because of new behaviour: `test_readyz` (restored a stale hard-coded revision; it already failed on the base commit), `test_lifecycle` and `test_billing_kafka` (outbox now also holds `entitlements.changed`), `test_ops` (grant list), three unit assertions (topic list, `j9:1`, `yt7:1`).
9. Added small config: `BillingConfig.grace_period_seconds`, `poll_after_seconds` (30 s with `SIM_FAST_JOBS`), `simulation`; `transition()` takes `grace_seconds`.
10. `.env.example` did not exist; created.

## Decisions needed

- Is `-1` for unlimited in `/me` right for the frontend, or keep `null` and add `limits_v2`?
- Catalog changes for Free users: accept that they are not announced, or add a user list?
- Simulator state is not cleaned up by any job (tables only grow); fine for staging?

## Cleanup note

My exit-check API (port 18080) and worker (port 9100) processes were started from the shell and may still be running; a gRPC port 50051 on your machine is held by something else, so I used 55051.
