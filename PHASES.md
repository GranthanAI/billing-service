# billing-service: coding phases

Derived from `docs/hld.md` (HLD) and `docs/lld.md` (LLD). The repo is greenfield, so Phase 0 creates the skeleton.

**Release mapping** (HLD "Build order"): Phases 0–9 = **v1** (Free, Pro, Max paid launch). Phase 10 = **v2** (organisations). Phase 11 = **v3** (Pay as you Go wallet). Phase 12 = post-launch scale items, only when measured.

**Ground rules for every phase**
- Domain code is pure (no I/O); use cases take ports; adapters implement ports. import-linter enforces this from Phase 0.
- Money is `int` paise / micro-rupees. No `float`. Time comes from an injected `Clock`.
- Every phase ends green on: ruff, mypy `--strict`, import-linter, its own tests. A phase is not done until its **Exit check** passes.
- No live third-party calls in automated tests (no Razorpay, no LLM providers): use a fake `PaymentGateway` and fake provider payloads. Real Postgres, Valkey and Kafka run through Testcontainers. The Razorpay test-mode run is a manual/staging step, not part of the default suite.
- Each job handler is built in the phase of the feature that needs it; the runner and scheduler exist from Phase 4.
- Where the docs disagree: LLD Decisions section, then the rest of the LLD, then the HLD (see `AGENTS.md`).
- Migrations are expand-then-contract (Alembic).

---

## Phase 0: Skeleton and tooling

**Goal:** an empty service that builds, lints, boots and passes `/healthz`.

- `pyproject.toml` (uv), Python 3.12; deps from LLD "Libraries"; dev deps: ruff, mypy, import-linter, pytest, Hypothesis, Testcontainers, time-machine.
- Layout per LLD: `src/billing/{domain,app,adapters,api,jobs}`, `main_api.py`, `main_worker.py`, `settings.py`, `observability.py`, `tests/{unit,property,integration,contract,e2e}`.
- import-linter contracts from the LLD layer table (domain imports stdlib only; app never imports adapters/api/FastAPI/SQLAlchemy; adapters never import api).
- `settings.py` (pydantic-settings), all settings from LLD "Settings"; fail fast when a required value is missing.
- `GET /healthz`, `GET /readyz` stub; structlog JSON logging with `correlation_id`; Prometheus `/metrics`.
- Dockerfile (one image, two commands: api, worker), `docker-compose.yml` with Postgres 17, Valkey 8 (AOF on, `noeviction`), Kafka (KRaft).
- Makefile targets: `up`, `down`, `test`, `lint`, `migrate`, `catalog-sync`.
- CI workflow: lint, type-check, import-linter, tests, image build.

**Exit check:** `make up` starts the stack; `/healthz` returns 200; CI is green on an empty test suite.

---

## Phase 1: Domain core (pure Python)

**Goal:** all money, rating and state rules, fully unit- and property-tested, with no database.

- `domain/money.py`: `Paise`, `MicroInr`, `round_half_up`, `split_gst` (CGST/SGST vs IGST).
- `domain/catalog.py`: `PlanVersion`, `Limits`, `ModelGroup`, `Interval`, `OwnerType`, `OrgVariant`.
- `domain/entitlements.py`: `Grant`, `Entitlement`, `pick` (override > highest rank > org on tie > payg > free), content hash.
- `domain/subscription.py`: status enum (`incomplete`, `active`, `past_due`, `suspended`, `cancelling`, `ended`), transition table, `IllegalTransition`, Razorpay status mapping.
- `domain/rating.py`: tokens to `cost_micro_inr` and billable units (`ceil(input / 32k)`), `Decimal` rounded once.
- `domain/proration.py`: `unused_credit`, upgrade/downgrade classification (cheaper total = downgrade).
- `domain/ledger.py`: `Posting`, posting builders (subscription charge, refund, top-up, payg usage, expiry/breakage, settlement), balance check.
- `app/ports.py`: Protocols for Repos, Counters, PaymentGateway, Clock, Publisher, ObjectStorage.
- `Clock` and `TestClock`.

**Tests:** transition table row by row; the LLD worked examples (Pro ₹499 GST split 42288 + 3806 + 3806; Haiku 5.5 message = 118,750 micro-INR at ₹95/US$; Pro→Max proration ₹249.50). Hypothesis properties: GST parts sum to gross, taxable × 1.18 within 1 paisa; every built ledger transaction sums to zero; `pick` never returns a lower rank than another live non-override grant; `unused_credit` between 0 and the amount paid and falls over time.

**Exit check:** 95% line coverage on `domain/`; properties pass at 500 examples.

---

## Phase 2: Database schema and repositories

**Goal:** the `billing` schema in Postgres with its guards, plus repository adapters.

- Alembic migrations in dependency order: enums → catalog/accounts → subscriptions/overrides → usage (partitioned `usage_events`, `usage_daily`) → money/ledger (`payments`, `refunds`, `invoice_counters`, `invoices`, `invoice_lines`, `ledger_*`, `credit_lots`) → plumbing (`webhook_inbox`, `processed_events`, `outbox`, `idempotency_keys`, `jobs`, `admin_audit`).
- DB guards: `forbid_change()` triggers on `ledger_entries`, `ledger_transactions`, `usage_events`; deferred constraint trigger `assert_ledger_balanced`; `REVOKE UPDATE, DELETE` for `billing_app`; roles `billing_migrator` / `billing_app`.
- `adapters/db/tables.py` (SQLAlchemy Core), `repos/*.py`, unit-of-work (one transaction per use case) with the outbox write in the same transaction.
- UUIDv7 id helper.
- `partitions.ensure` logic (as a callable; scheduled in Phase 8).

**Tests (Testcontainers Postgres 17):** migrations up from empty; `UPDATE`/`DELETE` on ledger and usage rows rejected; unbalanced ledger transaction fails at commit; the unique partial index allows only one live subscription per billing account; partition create/attach works.

**Exit check:** integration suite green against a real Postgres.

---

## Phase 3: Catalog loader and pricing data

**Goal:** `plans.yaml` is the source of truth and lands in `plan_versions`; model prices and FX are managed with effective dates.

- `config/plans.yaml` exactly as in the LLD (free, pro, max now; org and payg blocks present but unused until v2/v3).
- Pydantic validation: every limit present, non-negative prices, unique ranks per audience, every listed model has a price.
- `billing catalog sync` CLI: checksum per plan/variant, new `plan_versions` row at `version + 1` for a changed checksum, `retired_at` on the old row, skip unchanged. Flag limit-only changes for the admin "apply to existing" action.
- Razorpay plan creation for each new priced version and interval via the `PaymentGateway` port; store the IDs.
- `model_prices` and `fx_rates` repos with "as of timestamp" lookup (`PriceBook.at`, `FxRates.at`). Seed script for initial model prices and FX.
- `GET /billing/v1/plans`.

**Tests:** unchanged checksum is a no-op; changed price creates version 2 and keeps version 1 intact; price lookup straddling the 1 Jan 2027 Gemini change returns the right row on each side.

**Exit check:** `make catalog-sync` against a fresh DB (fake gateway) is idempotent on a second run.

---

## Phase 4: Contracts and Kafka plumbing

**Goal:** typed events and the transactional outbox/inbox, before any feature emits events.

- Protobuf files in a `proto/` folder in this repo, with Buf (move to a shared `granthan-contracts` repo later): `BillingInternal` gRPC (`CheckAndReserve`, `Release`, `GetEntitlements`), the `Envelope`, `LlmCall`, `ChatResponseGenerated`, `UsageThresholdReached`, `SubscriptionChanged`, plus `PaymentFailed`, `InvoiceIssued`. `buf.gen.yaml` generates stubs; `buf breaking` in CI.
- Kafka producer (idempotent, `acks=all`) and consumer base class: offsets committed only after the DB transaction commits; retry 5× with backoff (0.5–8 s), then copy to `billing.usage.dlq`.
- Outbox relay: single replica via `pg_try_advisory_lock`, ordered, batch 500, 0.2 s idle sleep.
- `processed_events` claim helper (consumer inbox).
- Job runner and scheduler (LLD "Background jobs"): `FOR UPDATE SKIP LOCKED` claim, `locked_until`, exponential retry to `dead`, reaper for stuck `running` jobs, slot `dedupe_key`s. Handlers are registered by later phases.
- Worker entry point (`main_worker.py`) running consumers, relay and the job runner.

**Tests:** outbox rows publish in order and mark published; replayed batch is deduped by `processed_events`; poison message lands in the DLQ after 5 tries; Buf breaking check wired; two workers never claim the same job; a failing job retries then goes `dead`; the reaper requeues a killed job.

**Exit check:** with Testcontainers Kafka, a use case that writes an outbox row results in exactly one consumed event, even after a forced relay crash and restart.

---

## Phase 5: Valkey hot path (CheckAndReserve / Release)

**Goal:** the atomic limit check, the heart of the product. Target p99 under 10 ms.

- Lua scripts: `reserve.lua`, `commit.lua`, `release.lua` exactly per LLD (idempotent ALLOW replay, Best→Smart downgrade, late-usage counting, `GONE` on double release).
- `adapters/valkey/counters.py` (EVALSHA with NOSCRIPT fallback), key builders using the `{uid}` hash tag, IST-day and month keys.
- Entitlement resolver (`app/reserve.py`): `ent:{uid}` cache (10 min), Postgres fallback in order override → higher of org/personal → Free; invalidation on `subscription.changed` and `org.member.*`.
- `app/reserve.py` `CheckAndReserve` steps 1–5 (v1 uses plan mode only; the credits branch is stubbed behind a "no wallet" check until Phase 11), `Release`, `GetEntitlements`.
- `holds.sweep` job handler (every 30 s) via `holds:due`; the expire path gives the message back.
- Valkey rebuild routine: today's `use:*` from `usage_events`.
- gRPC server (`api/grpc/internal.py`) with service-token auth and `grpc.health.v1`; wired into `main_api.py` on port 50051 in the same event loop as FastAPI.
- Degraded-mode contract documented for conversation-service (Fast only, 10/day, deny Best/Ultra); billing exposes the deadline expectations from the LLD table.

**Tests:** 200 parallel reserves at limit 60 allow exactly 60; Hypothesis interleavings of reserve/commit/release/expire never exceed the limit and never go negative; two tabs racing at 59/60; idempotent retry returns the same hold; Max user's 13th Best request is downgraded to Smart, not denied; IST midnight reset (time-machine); rebuild after wiping Valkey reproduces counters.

**Exit check:** Locust run on local hardware shows 200 reserves/s with p99 under 10 ms.

---

## Phase 6: Subscriptions, checkout and webhooks

**Goal:** a user can pay for Pro/Max by card or UPI Autopay and get the plan.

- `adapters/razorpay/gateway.py` implementing `PaymentGateway` over httpx (timeouts, retries, lookup-by-`notes` before create); `signatures.py` (constant-time HMAC for webhook and checkout verify).
- Fake gateway for tests that records calls and scripts responses and webhooks.
- Billing accounts: lazily created per user, `state_code` required at checkout.
- Use cases: `checkout`, `checkout/verify`, `cancel`, `resume`, `change_plan` (upgrade = new mandate starting now + refund and credit note of unused days; downgrade = new mandate at `period_end`, pending plan applied at the boundary).
- `app/webhooks.py`: raw-body route stores to `webhook_inbox` (`ON CONFLICT DO NOTHING`), returns 200 fast; worker processes rows: fetch from Razorpay outside the transaction, lock subscription row, `record_charge`, `apply_transition`, ignore `IllegalTransition`. Backoff 30 s → 1 h over 10 attempts, then `failed` + alert.
- Idempotency middleware (`api/rest/idempotency.py`), RFC 9457 error handler with the LLD error codes.
- `record_charge`, built in full here: `payments` row, ledger postings (clearing / revenue / GST payable), invoice with gapless `GA/2627/000123` numbering from the locked `invoice_counters` row in the same transaction, CGST+SGST vs IGST by `state_code`, `GST_REGISTERED=false` producing a bill of supply, financial-year rollover on 1 April (IST).
- Refunds and credit notes (`CN` series) with balanced reversal postings, used by the upgrade flow (`refund.issue`).
- Job handlers: `webhook.process`, `checkout.reconcile`, `gateway.cancel`, `refund.issue`.
- Outbox events: `subscription.changed`, `payment.failed`. Cache invalidation of `ent:{uid}` on change.
- REST: `/checkout`, `/checkout/verify`, `/subscription/{change,cancel,resume}`, `/webhooks/razorpay`.
- Auth via api-gateway headers (`X-User-Id`, `X-Staff-Role`, `X-Correlation-ID`); never trust a client user id.

**Tests:** Razorpay's documented sample webhook payloads (replace with recorded real ones after the test-mode run) each applied twice, out of order, and with a bad signature; verify and webhook racing, whichever lands first wins and the other no-ops; invoice numbers gapless under rollback and concurrent creation, April 1 rollover, invoice CHECK (taxable + tax = total); upgrade refund issues a credit note and balanced reversal; upgrade and downgrade flows with the fake gateway; cancel from UPI app maps to `cancelling`; idempotency reuse, in-progress and replay cases.

**Exit check:** end-to-end with the fake gateway: checkout → verify → webhook → `active` → `subscription.changed` consumed → entitlement flips to Pro. One manual run in Razorpay **test mode** (card, UPI, failed charge, downgrade) before launch.

---

## Phase 7: Usage metering and rating

**Goal:** every answered message is rated and recorded exactly once.

- `billing-usage` consumer for `chat.response.generated`, `artifact.manim.rendered`, `file.youtube.ingested`.
- `app/usage.py` `apply_response`: claim in `processed_events`, rate at `occurred_at` prices and FX, insert `usage_events` (`ON CONFLICT DO NOTHING`), then `commit.lua` after the DB commit. `succeeded = false` releases the hold and records helper-call cost with `price_paise = 0`.
- Late event after hold expiry is counted and billed (via `commit.lua` branch).
- Threshold events at 80%/100% with `SET NX` guards (`thr:{uid}:...`) into the outbox: `usage.threshold_reached`.
- `usage.rollup` job handler (daily 00:30): idempotent full recompute of yesterday's `usage_daily`.
- Metrics: `billing_usage_cost_micro_inr_total`, `billing_usage_events_total`.

**Tests:** same event applied N times leaves the same rows; late event after sweeper expiry; the LLD worked-cost example end to end; failed generation does not charge; DLQ on a malformed event.

**Exit check:** replaying a day's recorded events reproduces `usage_daily` byte-for-byte on a second run.

**Cross-service dependency (v1 blocker, outside this repo):** llm-service must send provider-reported token counts, all `calls` with `purpose`, `hold_id`/`model_group`/`succeeded`, call `Release` on failure, use the granted group and `max_context_tokens`, and publish `artifact.manim.rendered`. conversation-service must call `CheckAndReserve` before publishing and implement the circuit-breaker degraded mode. Track these as separate tickets; this phase uses contract tests against the proto.

---

## Phase 8: Lifecycle and housekeeping jobs

**Goal:** the remaining scheduled work. The runner, scheduler and the handlers for earlier features already exist.

- Handlers: `grace.expire`, `period.close`, `subscription.poll`, `renewal.remind`, `partitions.ensure`, `housekeeping`.
- Register the full schedule from the LLD table (times in IST) in the scheduler.
- Handlers are idempotent; each has a test that runs it twice.

**Tests (time-machine):** `past_due` to `suspended` after 3 days; late successful charge restores the plan; `cancelling` to `ended` at period end; pending downgrade applied at the boundary; stuck `incomplete` ended after 24 h; missed webhook caught by `subscription.poll`.

**Exit check:** a simulated 40-day timeline (renewal, failed charge, grace, suspend, recovery, cancel) ends in the expected states with the expected events.

---

## Phase 9: Invoices, ledger and v1 surface

**Goal:** invoice PDFs, the user and admin APIs, reconciliation and observability. Completes **v1**. (Invoice numbering, GST split, ledger postings and credit notes are already done in Phase 6.)

- Settlement postings (bank, payment fees) via the daily job or admin-entered settlement.
- `invoice.render` job: Jinja2 + WeasyPrint, upload to object storage, emit `invoice.issued`. Presigned URL (5 min) redirect for `GET /invoices/{id}/pdf`.
- User REST: `GET /me`, `GET /me/usage`, `GET /invoices`.
- Admin REST (`billing_admin` write, `support` read; every write needs a reason and writes `admin_audit`): accounts search/detail, overrides create/end, refunds, `PUT model-prices` / `fx-rates`, `GET costs`, `GET reconciliation`.
- `reconcile.daily`: the four checks (Razorpay vs `payments`, ledger sums to zero and balances = entries, provider cost export vs `cost_micro_inr`, Valkey counters vs `usage_events`). Report only, never auto-correct. `billing_reconciliation_difference_paise` metric.
- `revenue.recognise` monthly job for yearly plans.
- Observability completion: all LLD metrics, alert rules, spans (`billing.reserve`, `billing.usage.apply`, `billing.webhook.process`, `razorpay.*`), PII redaction of Razorpay payloads in logs/Sentry.
- `user.deleted` consumer: cancel mandates, anonymise, keep invoices.
- Security pass: secrets via OpenBao/SOPS, DB role grants verified, webhook secret rotation note.

**Tests:** reconciliation detects an injected mismatch; PDF renders and `invoice.issued` is emitted; admin write without a reason is rejected.

**Exit check (v1 launch gate):** full test suite plus the Locust gate green; chartered accountant has signed off GST choices; Razorpay test-mode run complete; Terms/Privacy/Refund pages live (Razorpay activation); staging smoke test (`/readyz`, a staff `CheckAndReserve`, Razorpay test webhook).

---

## Phase 10: Organisations (v2)

**Goal:** colleges and companies buy seats by invoice.

- `adapters/orgs.py` client; consume `org.member.joined` / `org.member.left` (`billing-org`), clear members' `ent:*`.
- Org billing accounts; seat-based subscriptions on `org_pro` / `org_max` with college/company variants and minimum seats; yearly billing by invoice with Razorpay payment link or bank transfer (no autopay above ₹15,000).
- Org Free 30-day pilot (up to 25 members) through `plan_overrides`.
- Entitlement resolver adds org grants and overrides; tie goes to org; usage attributed to the granting billing account.
- Admin `mark-paid` endpoint to record bank transfers and activate the plan; GSTIN on invoices.
- Quarterly prorated true-up invoice for members added mid-term.
- Usage-by-member report endpoint for org-service from `usage_daily`.
- notification hook: suggest cancelling a personal plan the org plan covers (billing never cancels it).

**Tests:** precedence matrix (override / org / personal / free, ties); seat true-up proration; mark-paid idempotency; org member leaving flips entitlement.

**Exit check:** a pilot org goes Free → paid by bank transfer, with correct invoice and correct per-member usage report.

---

## Phase 11: Pay as you Go wallet (v3)

**Goal:** prepaid credits on top of any plan.

- `ledger_balances` with row-locked debits; `credit_lots` (12-month expiry, oldest-expiry first).
- Top-up: `POST /wallet/topups` creates a Razorpay order; on `payment.captured` create the lot, post the wallet liability (taxable value) and GST payable, issue a tax invoice.
- Reserve: enable the credits branch in `CheckAndReserve` (second `reserve.lua` call in `payg` mode after the plan denies); no-credit Fast allowance (10/day) only for users whose plan is Pay as you Go; `wal:{account}` mirror, spend cap with 80%/100% events.
- Usage: wallet debit inside the usage transaction (`debit` locks balance row, spends lots, one balanced ledger transaction); YouTube, Manim and storage-over-10 GB units.
- Jobs: `credits.expire` (breakage revenue, reminder 30 days ahead), `wallet.resync` every 5 min.
- Org wallet with shared balance and per-member cap; org minimum top-up ₹5,000.
- Rate limit ~30 messages/min is enforced at api-gateway; document it, no code here.

**Tests:** debit never leaves held negative; lots spent in expiry order; plan-first-then-credits ordering; expiry posts breakage correctly; resync repairs a deliberately drifted mirror; ledger invariants property test extended to wallet flows.

**Exit check:** a top-up → use → exhaust → top-up cycle reconciles to the paisa in the daily reconciliation.

---

## Phase 12: Scale and hardening (only on measured need)

- ClickHouse fed from Kafka when Postgres usage queries slow.
- Temporal for multi-day dunning/workflows when org flows outgrow the job table.
- Hyperswitch or a second `PaymentGateway` adapter for failover.
- Valkey Cluster (keys already use `{uid}` hash tags; split `holds:due` per slot).
- Decide MinIO (self-built images) vs SeaweedFS for object storage (open decision in HLD).
- Archive `usage_events` partitions older than 13 months to object storage.

---

## Open items that gate phases

| Item | Gates | Owner |
| --- | --- | --- |
| GST treatment sign-off (state, SAC code, top-up tax timing) | Phase 9 launch, Phase 11 | Chartered accountant |
| One Razorpay test-mode run of the downgrade flow | Phase 6 exit | You |
| llm-service and conversation-service changes | Phase 7 exit, launch | Other repos |
| Move protos from `proto/` to a shared `granthan-contracts` repo | After v1 | You |
| GST registration, supplier state code, GSTIN | Phase 9 settings | You |
| MinIO vs SeaweedFS | Phase 12 | You |
