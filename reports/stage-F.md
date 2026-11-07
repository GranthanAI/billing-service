# Stage F: organisations (Phase 10)

## Built
- Migration `0015_orgs`: `min_seats`, invoice-mode subscriptions (`billing_mode`, `pending_seats`),
  invoice links to subscriptions, payment-link columns, one-invoice-per-period index.
- OrgDirectory port with HTTP, Valkey-cached, none and fake adapters; org grants and overrides in
  entitlement resolution; org membership consumer, DLQ and `personal_plan_redundant` event.
- `OrgBilling`: subscribe (B2B GST invoice + Razorpay payment link), settle by webhook or bank
  transfer (idempotent), seat increase (prorated half-up invoice) and decrease (at renewal), renewal
  invoice 30 days ahead, past due with `ORG_GRACE_DAYS` when unpaid.
- Admin routes, service-token usage route, `org.renewal` daily job, reconciliation check 1 ignores
  bank transfers.

## Tests
Unit: 326 passed, coverage 96.65%, lint clean. Integration: 7 new in `test_orgs.py`, all pass.
Full `make test-all` not yet run twice.

## Deviations / not done
- Pilot rule `variant` on admin overrides not built.
- Usage report is JSON only (no CSV).
- mypy over `tests/` has pre-existing errors; only `src` is gated.

## Notes for later
Seat true-ups are booked straight to revenue (not deferred). Bank payments post to `asset:bank`.
