# Stage E: Phase 9, invoices, ledger completion, user and admin APIs, reconciliation, observability

**This completes v1 in code. It is the v1 launch gate: I stopped here.** Stages F (organisations)
and G (wallet) start only when you say so. What is left before launch is a list of things only you
can do, in `runbooks/v1-launch.md` (status at the end of this file).

## What changed

- **Migration `0014_ops_tables`:** `reconciliation_runs`, `provider_costs`, and
  `usage_events.provider_costs` (jsonb, cost per provider; see "Choices" below). Also
  `0013a_housekeeping_grants` from Stage D. No `0009`.
- **Invoice PDFs.** `invoice.render` renders with Jinja2 + WeasyPrint, stores
  `invoices/<fy>/<series>-<fy>-<serial>.pdf` in an S3-compatible bucket (`OBJECT_STORE_*`; an
  in-memory store when unconfigured), sets `pdf_object_key` and publishes `invoice.issued` once.
  `record_charge` and `issue_refund` queue the render. `invoice.backfill` (and
  `python -m billing.cli invoices backfill`) queues a render for every invoice without a PDF.
  The template carries: supplier name, address, GSTIN and state; the document title (Tax invoice,
  Bill of supply, Credit note); number and date; recipient name, GSTIN and state; place of supply
  (state name and code); lines with SAC, quantity and taxable value; the rate and amount of each of
  CGST, SGST, IGST; the total in figures and in words (Indian grouping, "Rupees … Only"); the
  original invoice on a credit note; "Computer-generated document; no signature required."
- **Settlements.** `settlements.sync` (daily 06:00 IST) reads
  `GET /settlements/recon/combined?year&month&day` through the new port method
  `list_settlement_items(day)` (endpoint and fields confirmed against Razorpay's reference) and
  posts `settlement(gross, fee, fee_gst)` per payment (key `settle:<payment id>`) and the new
  `refund_settlement` (Dr `asset:razorpay_clearing`, Cr `asset:bank`) per refund (key
  `settle-refund:<refund id>`). `POST /admin/billing/v1/settlements` is the manual fallback.
- **`revenue.recognise`** (1st of the month, 02:00 IST): the n-th run after a yearly period starts
  moves taxable/12 from deferred to recognised revenue (the 12th takes the rounding remainder), key
  `recognise:<subscription>:<yyyymm>`.
- **User REST** (`X-User-Id`): `GET /billing/v1/me` (LLD shape, `wallet: null`), `/me/usage`,
  `/invoices`, `/invoices/{id}/pdf` (owner check, then a 302 to a 5-minute link; 404 for someone
  else's invoice or while the PDF is still being prepared).
- **Admin REST** (`X-Staff-Role`: `billing_admin` writes, `support` reads; every write needs a
  reason and writes `admin_audit` with before and after): account search (email, user id, account
  id, gateway subscription or payment id, GSTIN, invoice number), account detail with this month's
  cost, revenue and margin, overrides create/end, refunds (bounded by the refundable remainder, via
  `refund.issue`), block/unblock, `PUT model-prices` and `fx-rates`, `GET costs?by=plan|provider|user`
  (user = the 20 heaviest), `GET reconciliation?date=`, `POST catalog/apply-limits/{audit_id}`, and
  the manual settlement. Admin `POST`s also need an `Idempotency-Key` (scope `admin:<staff id>`).
- **`reconcile.daily`** (03:00 IST, the previous IST day): four report-only checks writing
  `reconciliation_runs`, setting `billing_reconciliation_difference_paise{check}`:
  Razorpay payments vs `payments` vs the clearing account; ledger transactions sum to zero and
  balances equal their entries; provider cost (`provider_costs` imported with
  `python -m billing.cli costs import --provider P --file F.csv`, more than 5% apart is flagged);
  Valkey day counters vs `usage_events`. A check that cannot run is recorded as `error`.
- **`user.deleted` consumer** (`billing-user`, DLQ `billing.user.dlq`): new domain trigger
  `AccountDeleted` (any non-ended status to `ended`, cancel every mandate now), a waiting change
  mandate abandoned and cancelled, the account anonymised (email, legal name, gateway customer id;
  state code and GSTIN kept), the user's Valkey keys deleted. Invoices, payments, the ledger and
  `usage_events` stay. `runbooks/data-retention.md` (marked for CA review).
- **Observability.** The LLD metrics that were missing: `billing_webhook_inbox_backlog`,
  `billing_payments_total`, `billing_subscriptions`; plus `billing_dlq_messages_total` and
  `billing_daily_ai_budget_micro_inr`. Spans `billing.usage.apply`, `billing.webhook.process`,
  `razorpay.<method>_<resource>` (the reserve spans existed). A structlog processor and a Sentry
  `before_send` blank email, phone, VPA, card and account fields and mask email addresses in text.
  `RAZORPAY_WEBHOOK_SECRETS` (comma-separated) is accepted next to `RAZORPAY_WEBHOOK_SECRET` during a
  rotation. `/readyz` also checks Kafka (cached 15 s).
- **Alert rules** `deploy/prometheus/billing-alerts.yml` (15 rules) with `promtool` unit tests.
- **Local check targets** (no CI changes, `.github/workflows/` untouched): `make audit` (pip-audit),
  `make alerts-check` (promtool through Docker), `make proto-check` (Stage B).
- `runbooks/v1-launch.md` (the full checklist) and `runbooks/data-retention.md`.

## Choices the docs did not spell out, and deviations

- **Cost per provider needed a column.** A message's helper calls can come from another provider
  than its answer (the workbook example: Claude answer, Groq helper), so a per-provider total from
  one `model` column would be wrong by up to 25%, far over the 5% tolerance. `usage_events` now
  stores `provider_costs` (jsonb, micro-rupees); rows without it fall back to the answering
  model's provider. Manim render and YouTube ingest estimates are filed under `internal`.
- **Settlement fee split.** Whether Razorpay's `fee` already includes the GST in `tax` is not
  something I could confirm without a real settlement. `settlements.sync` therefore derives the
  split from the bank credit: held back = gross − credit, of which `tax` is GST, the rest fee. This
  balances either way. **Verify against a real test-mode settlement** (listed in the runbook). Note
  this also means `payments.fee_paise` (stored from the payment entity) may double-count GST if it
  includes it; nothing reads it yet.
- **Revenue recognition ignores upgrade refunds.** A yearly plan upgraded mid-year restarts its
  period; the unrefunded remainder of the old period's deferred revenue is not released by
  `revenue.recognise`. The ledger reconciliation will show the balance but it is not wrong, just
  unreleased. Needs a decision (release on upgrade) before yearly upgrades are common.
- **Recipient address.** We hold only the customer's state, so the PDF prints name, GSTIN and state
  (also as place of supply), not a postal address. Ask your CA whether a B2C invoice needs one.
- **`GET /me`** reports `status: "free"` for a user with no subscription, and limits of `-1` as
  `null`.
- **Valkey-counters reconciliation** reports its difference in *messages*, not paise (the metric
  name is the LLD's). Rows with `billable_units = 0` (failed attempts) are not messages.
- **"Heavy user" alert** is not a Prometheus rule (it needs a per-user series); use
  `GET /admin/billing/v1/costs?by=user`. "Billing unreachable" is `up == 0` here; the callers'
  breaker signal lives in conversation-service. "AI spend" is a rolling 24 hours, not the IST day.
- **A reconciliation `error` row** is written when a check cannot run, but only the `difference`
  status carries a number; the alert fires on `billing_reconciliation_difference_paise != 0`, so
  an `error` does not page by itself. Worth a rule once you see how often it happens.
- **Admin refunds are asynchronous** (`refund.issue` job), so `POST /refunds` returns 201 "queued"
  with a `refund_key`.
- **The unbalanced-ledger check cannot be exercised by injection**: the database refuses to commit
  an unbalanced transaction (a test proves it), so that half of the check is a defence in depth.
- **`pip-audit`** reported no known vulnerabilities; it cannot audit the project itself (not on PyPI).

## Tests added in this stage

Unit: words and Indian grouping, `refund_settlement`, `AccountDeleted` (and the transition table),
redaction (logs, Sentry), webhook-secret rotation, spans on the Razorpay adapter.
Integration: invoice PDFs (text extracted from real PDFs: every required field, IGST, credit note,
bill of supply, once-only `invoice.issued`, backfill, CLI), settlements and refund settlements
(balanced, idempotent, manual fallback), revenue recognition (12 months sum to the taxable amount,
to the paisa), `/me` `/me/usage` `/invoices` and the PDF link (owner only), the whole admin API
(roles, reasons, audit, replay), reconciliation (each check catches an injected mismatch),
`user.deleted` (service and Kafka), gauges, counters, spans, the app role's grants, append-only
tables, the Kafka probe and `/readyz`. Alert rules: 11 `promtool` cases.

## For whoever picks up Stages F and G

- `OrgDirectory`, org subscriptions and the `invoice` billing mode will reuse `InvoiceService`
  (the template already shows a recipient GSTIN) and `SettlementService` (payment links settle the
  same way).
- The wallet's top-up payments need `GatewayPayment` to carry `order_id` (it does) and a
  `purpose='topup'` branch in `record_charge`.
- `reconcile.daily` check 1 compares *all* captured payments, so top-ups will be included
  automatically once they are recorded in `payments`.

## Exit check

Run on a laptop with very little CPU, so the slow Docker-backed suite was kept to the minimum:

- `make test` was changed to be **fast and unit-only** (unit and property tests, no Docker). The
  Docker tests moved to `make test-integration`; `make test-all` runs both.
- `make lint`: clean. `make proto-check`, `make alerts-check` (15 rules, all unit cases) and
  `make audit` (no known vulnerabilities): all passed.
- `make test`: **271 passed, domain coverage 97.4%** (about 2 minutes here), run twice (results below).
- Integration tests: every new Stage E file passed when written (invoice PDFs 8, settlements and
  revenue 9, `/me` 4, admin API 10, reconciliation 14, `user.deleted` 5, ops 8). After the last code
  changes I re-ran the ones those changes could affect: schema and migrations, `/readyz`, and worker
  startup (21 passed). **The full integration suite was not run end to end for Stage E**, and a run
  that started at the end was cut off when the session ended. Run `make test-integration` once on a
  faster machine before launch; it is on the launch checklist.
- Load gate: still skipped by instruction; it is a launch blocker.

## v1 launch checklist status (`runbooks/v1-launch.md`)

| Section | Status |
| --- | --- |
| 1. Blockers: load gate, Razorpay test-mode run, recorded fixtures, CA sign-off | **Open, all need you** |
| 2. Code gates | lint, proto-check, alerts-check, audit, `make test`: green. `make test-integration` in full and the `docker build`: not run on this laptop |
| 3. Accounts, legal, configuration | Open, needs you (legal pages, KYC, live keys, object store, Sentry, secrets) |
| 4. First-deploy order | Written, not exercised |
| 5. Fire each alert once on staging | Open, needs staging. The rules themselves are unit-tested with `promtool` |
| 6. Backups and a tested restore | Open, needs you |
| 7. Staging smoke test | Open, needs staging |
