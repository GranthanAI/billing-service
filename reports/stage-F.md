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

## Follow-up (done after the first Stage F commit)
- Pilot rule enforced in `AdminService.create_override` for organisations: at most 30 days and,
  through `OrgDirectory`, at most 25 members (refused when org-service cannot be asked). Org
  overrides take an optional `variant` (taken from org-service when omitted).
- `GET /internal/orgs/{id}/usage?format=csv` (user_id, day, model_group, messages, billable_units).
- Seat true-ups are deferred revenue, recognised monthly over the months left in the period
  (`revenue.recognise`), and released if the subscription ends early.

## Tests
Integration `test_orgs.py`: 11 tests, including the exit scenario (pilot override, conversion to a
paid subscription, bank transfer, B2B tax invoice with the org's GSTIN and a CGST/SGST split, bank
asset posting, usage report) and a CSV report test. Full suite results are in the Stage G report
(same runs).

## Deviations / notes
- mypy over `tests/` has pre-existing errors; only `src` is gated.
- Bank payments post to `asset:bank`; reconciliation check 1 ignores `bank_transfer` payments.
