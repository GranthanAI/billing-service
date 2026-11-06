# v1 launch checklist

Tick an item only when you have seen it, not when you expect it. Items marked **(you)** cannot be
done by the code: they need a person, an account or a staging environment.

## 1. Launch blockers (nothing ships until these are ticked)

- [ ] **Load gate green: p99 < 10 ms, 0 errors, at 200 reserves/s for 2 minutes, on a quiet
      Linux machine or in CI (you).** Not met on the Windows development machine (Stage A,
      `reports/stage-A.md`): the Phase 5 commit fails the same way there, so the cause is the
      machine. `make loadtest` on a quiet Linux host; stop unrelated containers first. Do not
      launch on an unmeasured hot path.
- [ ] **Razorpay test-mode run complete (you)** following `runbooks/razorpay-test-mode.md`: card,
      UPI Autopay, renewal, failed charge, cancel, resume, upgrade with refund, downgrade.
- [ ] **Recorded fixtures swapped in.** Real webhook bodies saved under
      `tests/fixtures/razorpay/recorded/`, the suite re-run against them, and every difference
      from the documentation samples fixed. The list of things to look for is at the end of the
      test-mode runbook (notably: does `payment.fee` include the GST in `payment.tax`?
      `settlements.sync` derives the fee from the bank credit and logs `settlement_fee_disagrees`
      when that differs from the stored fee + GST by more than a paisa; any such warning in the
      test-mode run means the stored split is wrong).
- [ ] **Chartered accountant sign-off (you)** on: the 18% GST on subscriptions and SAC 998319; the
      invoice and credit-note content (`adapters/templates/invoice.html`, GST rule 46); place of
      supply = the customer's state; paise rounding (half-up, rounded to the rupee only in returns);
      bill of supply when unregistered; the supplier state (`SUPPLIER_STATE_CODE`); the retention
      and erasure rules in `data-retention.md`; yearly plans booked to deferred revenue and
      released monthly (`revenue.recognise`).

## 2. Code gates (run on the commit you intend to ship)

- [ ] `make lint` clean (ruff, mypy --strict, import-linter)
- [ ] `make test` green **twice in a row** (unit and property tests, no Docker, domain coverage >= 95%)
- [ ] `make test-integration` green (real Postgres, Valkey and Kafka; needs Docker and a machine with
      CPU to spare: about 7 minutes on a normal laptop, much longer on a slow one). Run it in full at
      least once on the commit you ship; it was run file by file while Stage E was built.
- [ ] `make proto-check` (buf lint and breaking)
- [ ] `make alerts-check` (15 alert rules and their unit tests)
- [ ] `make audit` (no known vulnerabilities)
- [ ] `docker build -t billing-service .` succeeds (WeasyPrint needs Pango; the Dockerfile installs it)

## 3. Accounts, legal and configuration

- [ ] Terms of service, Privacy policy and Refund policy pages live (Razorpay activation needs them) **(you)**
- [ ] Razorpay KYC complete and the account activated for live payments **(you)**
- [ ] Live keys set: `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`; the live
      webhook points at `https://<host>/billing/v1/webhooks/razorpay` for `subscription.*`,
      `payment.*` and `refund.*` **(you)**
- [ ] `GATEWAY_SHARED_SECRET` set in billing and api-gateway sends it as `X-Gateway-Secret` on every
      request (see `contracts/api-gateway.md`); the process refuses to start without it. api-gateway
      must also strip client-supplied `X-User-Id` / `X-Staff-Role` and set `X-Staff-Role` only from
      staff claims in the JWT **(you, in api-gateway)**
- [ ] `ENVIRONMENT=production` and `PAYMENT_GATEWAY=razorpay` (the fake gateway refuses to start in
      production)
- [ ] Supplier details: `SUPPLIER_LEGAL_NAME`, `SUPPLIER_ADDRESS`, `SUPPLIER_GSTIN`,
      `SUPPLIER_STATE_CODE`, `GST_REGISTERED`, `INVOICE_SAC_CODE`
- [ ] Object store for invoice PDFs: `OBJECT_STORE_ENDPOINT`, `OBJECT_STORE_BUCKET`,
      `OBJECT_STORE_ACCESS_KEY`, `OBJECT_STORE_SECRET_KEY` (without them PDFs live in memory and are
      lost on restart)
- [ ] `SENTRY_DSN` set (errors are scrubbed of email, phone, card and VPA before they leave)
- [ ] `DAILY_AI_BUDGET_INR` set to the real budget (the AI-spend alert reads it)
- [ ] Secrets come from OpenBao / SOPS, not a plain `.env`; database roles are the Phase 2 ones
      (`test_the_app_role_has_exactly_the_grants_the_design_allows` guards the grants)
- [ ] Kafka topics exist, including `chat.response.cancelled` and `subscription.renewal_upcoming`

## 4. First deploy, in order

1. `alembic upgrade head` as the migrator (0010 to 0014 are new since Phase 5)
2. `python -m billing.cli pricing seed`, then `python -m billing.cli catalog sync` (Razorpay plans are
   created on the live account; each priced version gets its own)
3. If the sync reports a **limits-only** change, decide whether existing subscribers get it, then
   `POST /admin/billing/v1/catalog/apply-limits/{audit_id}` with a reason
4. Start `billing-worker` first (webhook processor, jobs, consumers), then `billing-api`
5. `GET /readyz` is 200 (Postgres at the migration head, Valkey configured and scripts loaded, Kafka
   reachable)
6. `python -m billing.cli invoices backfill` once, if any invoice was issued before the object
   store was configured (it queues a render for every invoice without a PDF)

## 5. Alerts: fire each once on staging **(you)**

`deploy/prometheus/billing-alerts.yml`. For each, trigger the condition and see the page or ticket:

| Alert | How to trigger it on staging |
| --- | --- |
| BillingReserveSlow | run `make loadtest` against a throttled staging, or inject latency |
| BillingDown | stop `billing-api` for 2 minutes |
| BillingHoldsLeaking | reserve without sending usage events until 2% of reserves expire |
| BillingUsageNotBilled | publish a `chat.response.generated` with an unknown model |
| BillingUsageLagging | pause the `billing-usage` consumer with traffic flowing |
| BillingUserEventsDeadLettered | publish a malformed `user.deleted` |
| BillingWebhookFailed | make `fetch_subscription` fail 10 times for one inbox row |
| BillingWebhooksSilent | (24 h) point Razorpay's webhook elsewhere on staging |
| BillingWebhookUnexpected | send a `subscription.completed` for the current mandate |
| BillingRefundFailed | Razorpay test mode: fail a refund |
| BillingOutboxStuck | stop Kafka for a minute |
| BillingPaymentsFailing | 20+ failing test payments in an hour |
| BillingMoneyMismatch | edit a `ledger_balances` row on staging, run `reconcile.daily` |
| BillingAiSpendOverBudget | set `DAILY_AI_BUDGET_INR` very low |
| BillingJobsDying | enqueue a job of an unknown kind |

"Heavy user" from the LLD is not a Prometheus alert (it needs a per-user series): look at
`GET /admin/billing/v1/costs?by=user`.

## 6. Backups and restore **(you)**

- [ ] Postgres backups with point-in-time recovery for the `billing` schema
- [ ] **A restore tested**: restore into a scratch database, run `alembic current`, then
      `reconcile.daily` for yesterday; the ledger check must be `ok`
- [ ] Valkey: AOF on, `noeviction` (`/readyz` refuses otherwise); after a loss, `python -m billing.cli
      valkey rebuild` rebuilds today's counters from `usage_events`
- [ ] Object store versioning or replication for the invoice PDFs (they can be re-rendered from the
      database with `invoice.backfill` after clearing `pdf_object_key`, but the issued PDF is the
      document the customer received)

## 7. Staging smoke test **(you)**

- [ ] `GET /readyz` is 200
- [ ] A staff user's `CheckAndReserve` over gRPC allows, `Release` gives it back
- [ ] Razorpay's "send test webhook" button gives a 200 and an `ignored`/`processed` inbox row
- [ ] A test-mode checkout end to end: invoice PDF downloads through `GET /billing/v1/invoices/{id}/pdf`
- [ ] `GET /billing/v1/me`, `/me/models`, `/models` answer
- [ ] An admin write without a reason is refused (422), with one it is audited

## 7b. Operating notes

- **Webhook secret rotation.** Add the new secret to `RAZORPAY_WEBHOOK_SECRETS` (comma-separated,
  secrets must not contain commas) and redeploy; both secrets now verify. Change the secret in
  Razorpay's dashboard; once nothing is signed with the old one, remove it and redeploy.
- **Reconciliation** runs at 03:00 IST for the previous day and never corrects anything. Read the
  results at `GET /admin/billing/v1/reconciliation?date=`. Import provider invoices with
  `python -m billing.cli costs import --provider <name> --file <csv>` (`day,cost_inr`).
- **Dead letters.** `billing.usage.dlq` and `billing.user.dlq` page. Fix the cause (usually a model
  price: `PUT /admin/billing/v1/model-prices`), then
  `python -m billing.cli dlq replay --topic billing.usage.dlq`.
- **A manual settlement** (when `settlements.sync` is missing a payment):
  `POST /admin/billing/v1/settlements` with the fee and GST from the bank statement.

## 8. CI/CD (later)

Development comes first, so none of this is automated yet and `.github/workflows/` is untouched.
Before launch these checks should become a pipeline. Today each runs locally:

| Check | Local command | Stage |
| --- | --- | --- |
| Lint, types, import rules | `make lint` | every stage |
| Unit, property and integration tests (Docker) | `make test` | every stage |
| `buf lint` and `buf breaking` for `billing.proto` | `make proto-check` | B |
| Generated protobuf code is current | `make proto && git diff --exit-code src/billing/gen` | B |
| Dependency vulnerability audit | `make audit` | E |
| Alert rules and their unit tests | `make alerts-check` | E |
| Load gate (see section 1) | `make loadtest` on a quiet Linux machine | launch |
| Container image build and scan | `docker build -t billing-service .` | launch |
| Existing `ci.yml` | already in the repo; extend it rather than replace it | launch |
