# Razorpay test-mode run (Phase 6 exit step)

One manual run against **staging** with Razorpay **test mode** keys. It is the only place the
real Razorpay API, real webhooks and real UPI/card flows are exercised; every automated test
uses the fake gateway. Do this before launch, and again after changing the gateway adapter.

Time: about 90 minutes, mostly waiting for renewals. Record the date, the commit SHA and who
ran it at the bottom.

## Before you start

- [ ] Staging runs the commit under test, migrated to `0011_bill_of_supply` or later.
- [ ] `PAYMENT_GATEWAY=razorpay`, `RAZORPAY_KEY_ID=rzp_test_…`, `RAZORPAY_KEY_SECRET`,
      `RAZORPAY_WEBHOOK_SECRET` set (test-mode values). `ENVIRONMENT` is not `production`.
- [ ] `SUPPLIER_STATE_CODE` is `10` (or your registered state) and `GST_REGISTERED=true`.
- [ ] `billing catalog sync` has run: `plan_versions.razorpay_plan_month/year` hold real
      `plan_…` ids for Pro and Max. Check in the Razorpay dashboard (test mode > Plans).
- [ ] Razorpay dashboard > Webhooks points at `https://<staging>/billing/v1/webhooks/razorpay`
      with the test webhook secret. Tick: `subscription.*`, `payment.*`, `refund.*`.
- [ ] Worker is running (webhook processor, job runner). `GET /readyz` is 200.
- [ ] A test user id (UUID) to send as `X-User-Id`. Use a **new** user per scenario unless the
      step says otherwise.
- [ ] Short renewals: in test mode create the Pro/Max test plans with a **daily** period, or
      use Razorpay's test clock if enabled for your account, so renewal does not take a month.
      Say which one you used in the log.

How to call the API (replace host, user and keys; a new `Idempotency-Key` for each call):

```bash
curl -s -X POST https://<staging>/billing/v1/checkout \
  -H "X-User-Id: $USER_ID" -H "Idempotency-Key: $(uuidgen)" -H "Content-Type: application/json" \
  -d '{"plan_code":"pro","interval":"month","state_code":"10","email":"you@example.com"}'
```

The response holds the `razorpay` block for Razorpay Checkout. Open Checkout with those options
(the frontend, or a small HTML page loading `https://checkout.razorpay.com/v1/checkout.js`).
Test cards and UPI ids: <https://razorpay.com/docs/payments/payments/test-card-details/> and
<https://razorpay.com/docs/payments/payments/test-upi-details/>.

Useful queries (staging database, read-only):

```sql
SELECT status, plan_version_id, current_period_start, current_period_end, grace_until,
       gateway_subscription_id, next_gateway_subscription_id
  FROM billing.subscriptions WHERE billing_account_id = (SELECT id FROM billing.billing_accounts WHERE owner_id = :user);
SELECT number, kind, taxable_paise, cgst_paise, sgst_paise, igst_paise, total_paise FROM billing.invoices ORDER BY number;
SELECT status, event_type, attempts, last_error FROM billing.webhook_inbox ORDER BY id DESC LIMIT 20;
SELECT kind, status, attempts, last_error FROM billing.jobs ORDER BY id DESC LIMIT 20;
```

## Evidence: save every real webhook

For **each** scenario below, copy the raw body of every webhook Razorpay delivered from
Razorpay dashboard > Webhooks > the delivery log (or `SELECT payload FROM billing.webhook_inbox`)
into `tests/fixtures/razorpay/recorded/<event>__<scenario>.json`, for example
`subscription_charged__card_first_charge.json`. Remove nothing but real customer data (email,
contact). These replace the documentation samples in `tests/fixtures/razorpay/*.json`.
Afterwards re-run the Phase 6 suite against the recorded files and fix whatever differs.

## Scenarios

For each: do the action, wait for the webhooks (up to a minute), then check **State**,
**Invoice** and **Ledger**.

### 1. Card checkout (Pro, monthly)

- Action: `POST /checkout` as user A, pay in Checkout with test card `5267 3181 8797 5449`
  (any future expiry, any CVV, OTP `1234` on the bank page). Then `POST /checkout/verify` with the
  three values Checkout returned.
- State: subscription `active`; `current_period_start/end` match Razorpay's `current_start/end`;
  `GET /billing/v1/plans`-independent check: the entitlement is Pro (`GetEntitlements` over gRPC,
  or the `ent:{uid}` key is absent/refreshed in Valkey).
- Invoice: one `GA/<fy>/000001` tax invoice, `total_paise = 49900`, taxable 42288, CGST 3806 +
  SGST 3806 for a Bihar customer (`state_code` 10), IGST 7612 for any other state.
- Ledger: transaction `charge:<pay_id>` sums to zero; clearing +49900.
- Also check: `payments.fee_paise` and `fee_gst_paise` are filled (Razorpay returns `fee`/`tax`).
  If they are null for subscription payments, note it: the fee only matters for Phase 9
  settlement postings.
- Webhooks to save: `subscription.authenticated`, `subscription.activated`,
  `subscription.charged`, `payment.authorized`/`payment.captured`.

### 2. UPI Autopay checkout (Pro, monthly)

- Action: new user B, `POST /checkout`, choose UPI in Checkout, pay with `success@razorpay`
  (UPI Autopay mandate). Verify as above.
- State and invoice: as scenario 1. The mandate is `method = upi` on the payment.
- Note whether `subscription.authenticated` arrives before or after `subscription.charged`; the
  service must end `active` either way.
- Webhooks to save: same set, with `method: upi`.

### 3. Renewal

- Action: user A, wait for the next daily/test-clock charge (or advance the test clock).
- State: still `active`; period advanced by one cycle; no `subscription.changed` event.
- Invoice: `GA/<fy>/000002` (gapless, no number skipped). Description shows the new period.
- Webhooks to save: `subscription.charged` (second).

### 4. Failed charge, grace, recovery

- Action: user C on a card that fails on renewal (test card `4000 0000 0000 0341` attaches, then
  declines on charge) or fail the payment from the dashboard test tools. Wait for
  `subscription.pending`.
- State: `past_due`, `grace_until` = failure time + 3 days; a `payment.failed` event is in the
  outbox and reaches Kafka; entitlement is still Pro.
- Then let Razorpay retry successfully (or replace the card). State returns to `active`,
  `grace_until` is null, a new invoice is issued.
- If Razorpay halts it (`subscription.halted`): state `ended`, a `gateway.cancel` job ran, the
  entitlement is Free.
- Webhooks to save: `subscription.pending`, `payment.failed`, `subscription.halted`.

### 5. Cancel

- Action: user A, `POST /subscription/cancel`.
- State: `cancelling`, access kept; a `gateway.cancel` job with `at_cycle_end: true` is `done`;
  in the dashboard the subscription shows a scheduled cancellation.
- At the end of the period Razorpay sends `subscription.cancelled`; the service must not change
  state (it is already `cancelling`). `period.close` (Phase 8) ends it; until then it stays
  `cancelling`.
- Variant: cancel from the customer side (UPI app / dashboard "Cancel") on user B: state goes to
  `cancelling` without any outgoing cancel call.
- Webhooks to save: `subscription.cancelled` (both variants).

### 6. Resume

- Action: user A (still `cancelling`), `POST /subscription/resume`, authorise the new mandate in
  Checkout.
- State: after `subscription.authenticated`, `active`, `next_gateway_subscription_id` set,
  `subscription_changes.status = authorised`. At the period end the new mandate's first
  `subscription.charged` swaps the mandates: `gateway_subscription_id` is the new one, next is
  null, change `applied`, invoice issued. The old mandate's `subscription.cancelled` changes
  nothing.
- Webhooks to save: `subscription.authenticated` for the future-start mandate, the swap
  `subscription.charged`.

### 7. Upgrade with refund (Pro to Max, monthly)

- Action: user D active on Pro with about half the period left. `POST /subscription/change`
  `{"plan_code":"max","interval":"month"}`, pay the new mandate in Checkout.
- State: after the new mandate's first charge, plan is Max immediately (entitlement), the period
  restarts that day, the change is `applied`. The old mandate is **cancelled immediately** in
  Razorpay (`gateway.cancel`, `at_cycle_end: false`).
- Invoice: a Max tax invoice for the full 149900, then a credit note `CN/<fy>/000001` for the
  unused share of the Pro payment (for 15 of 30 days: 24950; taxable 21144, CGST 1903, SGST 1903).
- Razorpay: a refund for that amount exists on the original Pro payment with
  `notes.refund_key`; the payment shows `partially_refunded` in billing; `refund.processed`
  arrives and `refunds.status` becomes `processed`.
- Ledger: `refund:upgrade:<change id>` reverses the share, sums to zero.
- Webhooks to save: `subscription.authenticated`, `subscription.charged` on the new mandate,
  `subscription.cancelled` (old), `refund.created`/`refund.processed`.

### 8. Downgrade (Max to Pro) — Decision 1 asks for one real run

- Action: user E active on Max. `POST /subscription/change` `{"plan_code":"pro","interval":"month"}`;
  authorise the new mandate (it starts at the current period end, so nothing is charged now).
- State after authorisation: still `active` on Max; `pending_plan_version_id` = Pro;
  `next_gateway_subscription_id` set; change `authorised`; a `gateway.cancel` job for the old
  mandate with `at_cycle_end: true` ran.
- Check in the dashboard that the new mandate shows `authenticated` with the right `start_at`.
- At the boundary the new mandate charges: plan becomes Pro, period advances, invoice issued for
  Pro, change `applied`. **Write down exactly what Razorpay sent and in which order** — the
  old mandate's `subscription.cancelled` and the new mandate's `activated`/`charged` — and
  whether the old mandate got one more charge (it must not).
- Webhooks to save: `subscription.authenticated` (future start), `subscription.activated`,
  `subscription.charged` (boundary), `subscription.cancelled` (old).

### 9. Checks that need no scenario

- [ ] Bad webhook signature: `curl` the webhook URL with a wrong `X-Razorpay-Signature` → 400;
      `billing_webhook_rejected_total` increases; no `webhook_inbox` row.
- [ ] Razorpay dashboard "send test webhook" → 200, one `webhook_inbox` row, processed or
      ignored without errors.
- [ ] `SELECT count(*) FROM billing.webhook_inbox WHERE status = 'failed'` is 0 and
      `billing.jobs` has no `dead` rows.
- [ ] Abandon a checkout (open Checkout, close it). After 15 minutes `checkout.reconcile`
      leaves it alone; after `CHECKOUT_ABANDON_HOURS` it is `ended` and cancelled in Razorpay.

## Where the real API may differ from our assumptions

Look for these while recording, and fix the adapter or the fixtures:

- `subscription.charged` payload: is `payment.entity.fee`/`tax` present, or null until settled?
- Does `GET /payments/{id}` carry anything linking it to the subscription (we use the event)?
- `GET /invoices?subscription_id=` shape, used by `checkout.reconcile` to find the first payment.
- Status of a future-start subscription after authorisation (`authenticated`, not `active`).
- What `cancel_at_cycle_end=1` returns, and the status/description when cancelling something
  already cancelled (the `gateway.cancel` handler treats a 4xx as done only if the subscription
  is then `cancelled`/`completed`/`expired`).
- Whether `refund.created` arrives and whether `refund.processed` can arrive before our
  `refund.issue` job has written the row (the processor retries until the row exists).

## Log

| Date | Commit | Run by | Result | Notes |
| --- | --- | --- | --- | --- |
| | | | | |
