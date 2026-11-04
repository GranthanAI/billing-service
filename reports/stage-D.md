# Stage D: Phase 8, scheduled jobs and the subscription lifecycle

## What changed
- **New domain trigger `NextMandateFailed`** (`domain/subscription.py`). Legal only from `active`
  with `next_gateway_subscription_id` set. It swaps the next mandate in, applies any pending plan
  and interval, sets `past_due` with `grace_until = now + GRACE_DAYS`, and emits
  `EmitPaymentFailed` (and `EmitSubscriptionChanged`, see below). In the transition table, the
  exactly-the-table-is-legal test, and the trigger derivation.
- **Trigger derivation** (`app/webhooks.py`): `subscription.pending`/`paused` on the *next* mandate
  gives `NextMandateFailed`; `halted` on the next mandate gives `NextMandateFailed` then
  `GatewayHalted`; `halted` on the *current* mandate of a still-`active` subscription gives
  `ChargeFailed` then `GatewayHalted` (the lost-`pending` case).
- **Jobs** (`app/lifecycle.py`, registered in `jobs/handlers/lifecycle.py`, scheduled in the
  worker):

  | Job | Schedule | Does |
  | --- | --- | --- |
  | `grace.expire` | every 15 min | `past_due` past `grace_until` gets `GraceExpired` (batches of 100, a row lock each) |
  | `period.close` | every 15 min | `cancelling` past `current_period_end` gets `PeriodEnded` |
  | `subscription.poll` | hourly | for live subscriptions whose period ended over 2 h ago: fetch the current and next mandate and apply what they show (charge, pending, paused, halted, cancelled) through `apply_gateway_event`; at most 5 Razorpay calls a second |
  | `renewal.remind` | daily 10:00 IST | `active` subscriptions renewing in [now+24 h, now+48 h): one `subscription.renewal_upcoming` per subscription per period (claimed in `processed_events` in the outbox transaction) |
  | `housekeeping` | daily 04:30 IST | `processed_events` > 30 d, published `outbox` > 7 d, `idempotency_keys` > 24 h, `done` jobs > 14 d, processed or ignored `webhook_inbox` > 180 d |

  `usage.rollup` (Stage C) and `partitions.ensure` already existed.
- **New event** `subscription.renewal_upcoming` (`billing_account_id`, `owner_id`, `plan_code`,
  `amount_paise`, `charge_at`), key = billing account. Topic added to `topics.py`, compose and the
  topic-list tests.
- **Migration `0013a_housekeeping_grants`:** `GRANT DELETE ON webhook_inbox TO billing_app` (Phase 2
  gave it no delete). Named `0013a` so the prompt's later numbers (0014 ops, 0015 orgs, 0016
  wallets) stay as planned.
- Test-only: the fake gateway accepts a plan period like `"10days"`, so a lifecycle fits in weeks.

## Deviations and choices the docs did not spell out
- **`NextMandateFailed` also emits `subscription.changed`.** The prompt lists only the payment
  failure; but the plan can change at the same moment (Max to Pro), so consumers and the entitlement
  cache need the change.
- **The poll only asks for payments when Razorpay's period moved past ours** (or for a next
  mandate with `paid_count > 0`), because listing a subscription's payments costs 1 + N Razorpay
  calls. It applies the *newest* unrecorded payment. If several cycles were missed in an outage, the
  older cycles' payments/invoices are not reconstructed (their period boundaries are not in the
  payment object); flagged here as a known limit.
- **Rate limit.** `RateLimiter(5/s)` spaces calls; a payments listing is charged one slot per
  payment it fetched, after the fact.
- **`period.close` does not apply pending downgrades** (the LLD one-liner mentions it). A downgrade
  is applied by the first charge of the next mandate, never by the clock, so nothing is left for
  `period.close` to do for them.
- **`renewal.remind` names the plan that will be charged**: a scheduled downgrade is already the
  plan and price in the reminder.
- **`housekeeping` `jobs` age is measured from `run_at`**; the table has no completion time.
- **Webhook inbox pruning** covers `processed` and `ignored` only; `failed` and `received` rows stay
  for the alert and for manual replay.

## Tests
- Domain: `NextMandateFailed` (swap, plan applied, grace, no pending plan, needs a next mandate), and
  the transition table test extended.
- Derivation: failures on the next mandate, halted on current while active, halted on next.
- `tests/integration/test_lifecycle.py` (14, on a test clock):
  grace expiry then late recovery, twice-run idempotence; period close on the minute; failed first
  charge of the next mandate (plan Pro, `past_due`, payment.failed, recovery); halted next mandate;
  poll recovers a missed charge (not before 2 h), a missed `pending`, a missed `halted`, a missed
  customer cancellation, and leaves healthy subscriptions alone (zero Razorpay calls); rate-limiter
  spacing; reminders once per period, in the 24-48 h window only, naming a scheduled downgrade and
  skipping cancelling subscriptions; housekeeping windows (kept inside, removed outside, unpublished
  outbox and unprocessed inbox rows kept); and the 40-day timeline.
- **The 40-day timeline** (10-day billing periods): signup on Max (D0), reminder (D9), renewal whose
  webhook is lost and recovered by the poll (D10), reminder (D19), failed charge (D20, `past_due`),
  grace still running (D22), grace over (D23, `suspended`, Free limits), Razorpay's retry succeeds
  (D24, `active`), downgrade to Pro authorised (D26), reminder naming the Pro price (D29), boundary
  charge on the new mandate (D30, plan Pro), cancel (D32, `cancelling`), period close (D40, `ended`).
  It checks the exact ordered outbox events (10), the six `subscription.changed` transitions, the
  state history, four consecutive invoices `GA/2627/000001`-`000004` (Max, Max, Max, Pro) and a
  balanced ledger.

## Exit check
See the end of this file.

## For later stages
- Stage E: `subscription.renewal_upcoming` is a produced topic like the others (alert rules and
  contract docs should list it). The poll's "missed several cycles" limit belongs in the
  reconciliation check (Razorpay payments vs `payments`).
- `housekeeping` will need to prune `reconciliation_runs` / `provider_costs` if Stage E decides to.

## Exit check (results)
- `make lint`: clean.
- `make test`: **402 passed, twice in a row** (411 s and 409 s); domain coverage 99.5%.
- The 40-day timeline test passes (states, events, invoices in order).
- Load gate: skipped by instruction (launch blocker in `runbooks/v1-launch.md`).
