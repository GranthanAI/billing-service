# Stage E2: fixes from review

## 1. REST trust boundary
- New setting `GATEWAY_SHARED_SECRET` (required; comma-separated to rotate; the process refuses to
  start without it). `GatewayAuthMiddleware` (`api/rest/gateway_auth.py`) answers **401** before any
  handler, idempotency record or database write unless `X-Gateway-Secret` matches one configured
  value (`hmac.compare_digest` against every value, so timing does not say which). With no secret
  configured it refuses everything (closed, not open).
- Public (no secret): `/billing/v1/plans`, `/billing/v1/models`, `/billing/v1/webhooks/razorpay`,
  `/healthz`, `/readyz`, **and `/metrics`**. `/metrics` was not in your list; I left it open because
  Prometheus scrapes it inside the cluster network and has no gateway secret. Say so if you want it
  behind the secret and Prometheus configured with it.
- FastAPI's own `/docs` and `/openapi.json` are protected like everything else.
- `contracts/api-gateway.md`: the gateway must send the header, strip client-supplied `X-User-Id`,
  `X-Staff-Role` and `X-Correlation-ID`, and set `X-Staff-Role` only from staff claims in the JWT.
- Tests: 43 unit cases (every protected path x GET/POST/PUT x missing / empty / wrong / near-miss
  secret; public paths; rotation; fail-closed; the setting) and one integration test on the real
  app: user and admin routes get 401 with no, wrong and empty secrets, nothing is written
  (`idempotency_keys`, accounts and `admin_audit` stay empty, no gateway call), a forged
  `X-Staff-Role` without the secret gets nothing, public routes still work.
- The integration harness (`env.api()`, the models test) now sends the secret.

## 2. Razorpay fee semantics
- `payments.fee_paise = fee - tax` and `fee_gst_paise = tax` (`fee_without_gst`: never negative; a
  missing tax leaves the fee as given). The fake gateway now produces a realistic fee that includes
  its 18% GST.
- Migration `0015a_fee_excludes_gst` fixes existing rows (`fee_paise - fee_gst_paise` where both are
  set and the fee is not smaller than the GST); the downgrade adds it back. Tested on a database
  migrated to 0014 with rows in each shape.
- `settlements.sync` still derives the split from the bank credit, and now logs
  `settlement_fee_disagrees` (with the stored and derived amounts) when that differs from the stored
  fee + GST by more than 1 paisa. The bank credit is still what gets posted. Tested: no warning when
  they match, none within 1 paisa, one warning (and the bank balance right) when Razorpay held back
  more, none when no fee is stored.

## 3. Deferred revenue (`app/revenue.py`; the revenue half of `settlements.py` moved here)
- (a) Slice keys are `recognise:<subscription>:<payment>:<n>`. Each run posts every missing slice
  from 1 to n, so a skipped month is caught up (the catch-up postings are dated at the catch-up run,
  not back-dated). Each slice is also capped at what is still deferred, so a partial refund can no
  longer drive deferred revenue negative (found while writing the tests: with the old rule the 12
  slices of the full amount would over-recognise after a partial refund).
- (b) `release_deferred(payment)` posts `revenue_recognition` of
  taxable - recognised - refunded taxable (key `release:<payment>`), only for yearly payments (read
  from the invoice period), and only if something is left. Called:
  - from `issue_refund` after an upgrade's refund and credit note are posted;
  - from the webhook applier when an upgrade charges and **no refund is being made** (the old
    period was used up), so that case does not leave the old payment deferred;
  - from `apply_transition` whenever a yearly subscription goes to `ended`. That covers
    `AccountDeleted`, the period ending (`PeriodEnded`), a halt, and an external cancel while
    suspended, not only the cases you listed.
- (c) Tests (`tests/integration/test_deferred_revenue.py`, 10): a skipped month catches up (slices
  1-3, then 4-5, never more than 12); a partial refund then 12 months never leaves a debit balance
  and ends at zero; release posts once and is a no-op for a monthly payment; a mid-year upgrade
  leaves the old payment at 0 deferred and the account's deferred balance equal to the new payment
  only; an upgrade at the period end (nothing to refund) releases at once; deleting a yearly account
  releases the rest (and a replay posts nothing more); deleting a monthly account posts no release;
  a yearly subscription that runs to its end, and one that is halted, release; the ledger balances
  throughout.
- Known limit: a yearly -> monthly downgrade at the boundary does not release the old yearly
  payment's last slice (the 12th monthly run normally posts it first, since runs are on the 1st at
  02:00 IST and the charge is later in the day).
- Not migrated: recognition postings with the old key format. Nothing is in production yet.

## 4. Admin refund interval
- `AdminService.refund` reads the interval from the refunded payment's invoice period
  (`domain.proration.period_interval`: more than 31 days is a year), not from the subscription.
  `issue_refund` does the same from the original invoice, so a refund is booked against deferred
  revenue or revenue according to what the payment paid for. Tests: a yearly payment whose
  subscription has since moved to monthly is refunded as `year` (deferred revenue reversed, revenue
  untouched); a monthly payment is booked against revenue; `period_interval` boundaries (31 vs 32 days).

## 5. Test run
See the end of this file.

## 5. Test run (`make test-all`, laptop as it was; no containers were stopped)

- `make lint`: clean.
- **`make test-all` green: 325 unit and property tests (domain coverage 97.4%, about 1 minute) and 226
  integration tests (about 5.5 minutes), 0 failed.** Total 551.
- The first full run had one failure: `test_lifecycle.py::test_a_40_day_timeline...`. It asserted the
  exact ordered event list from Stage D, and since Stage E every invoice also emits `invoice.issued`;
  I had not re-run that file after Stage E. The test now flushes the render jobs, checks that there
  are exactly four `invoice.issued` events, and compares the remaining events as before. Not a
  product bug. The suite was then re-run in full, and that run is the green one above.
