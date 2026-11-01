# GranthanAI billing-service HLD

Oct 30, 2026 · @ShubhChi

## Summary

Build a thin billing-service yourself in Python/FastAPI on PostgreSQL, Valkey and Kafka, with Razorpay only as the payment collector. Do not adopt Lago or Kill Bill: neither lists Razorpay or UPI Autopay as a native integration, so you would write the India-specific half anyway while running a second application stack.

The design follows what Stripe-scale billing teams do, sized for a solo founder at 500 to 12,000 users:

- **Six concerns, one service.** Catalog, entitlements, metering, rating, invoicing and ledger are separate modules with separate tables, so any one can later move out or be replaced.
- **One fast path, everything else async.** Only the limit check (`CheckAndReserve`) sits in front of a message; it runs as one atomic Valkey script. Usage, cost, invoices and alerts flow afterwards through Kafka.
- **Money is boring on purpose.** Integer paise, an append-only double-entry ledger, corrections by reversal, never by edit.
- **Every write is idempotent.** Idempotency keys on APIs, unique event IDs on webhooks and Kafka, and a daily reconciliation against Razorpay and the AI providers.
- **Prices are versioned and dated.** Plans and model prices carry effective dates, because your workbook already expects Gemini Flash prices to double on 1 Jan 2027.

## What billing must do for GranthanAI

The workbook's heaviest-user margins of 3–7% on Pro, Max and college Org Pro set the bar: a limit that leaks by even a few messages a day turns a plan unprofitable. Every requirement below comes from the plans workbook or the release plan.

| Requirement | Source sheet | Design consequence |
| --- | --- | --- |
| Free ₹0, Pro ₹499, Max ₹1,499 a month; yearly ₹4,990 and ₹14,990 | Individual Plans | Catalog with monthly and yearly prices per plan version |
| 25 / 60 / 80 messages a day; Max gets 12 Best-model messages, then switches to Smart | Individual Plans, Limit Behaviour | Per-day counters per model group; an over-limit Best request is downgraded, not denied |
| Per-plan limits on branches, messages per conversation, memory facts, reading size (16k / 64k / 128k), documents, file size, YouTube per month, storage, custom modes, instruction length | Individual Plans | An entitlement map that the owning services read and enforce; billing counts only what costs money |
| Pay as you Go: ₹0.20 / 0.50 / 4 / 12 per Fast / Smart / Best / Ultra message; one more message per extra 32k tokens read; ₹1 per YouTube video, ₹2 per Manim video, ₹10 per GB above 10 GB | PAYG Prices | A rating step that turns tokens into billable units and paise |
| Top-up packs ₹199 to ₹1,999 valid 12 months; organisation minimum ₹5,000 | PAYG Prices | Prepaid wallet with credit lots that expire |
| Spend cap with alerts at 80% and 100%; about 30 messages a minute | PAYG Prices | Threshold events; rate limit at the gateway |
| College vs company seat prices; minimum 50 / 10 / 5 seats; yearly billing; Org Free 30-day pilot up to 25 members | Organisation Plans | Seat-based subscriptions billed by invoice, plus pilot overrides |
| Monthly GST invoice with GSTIN | Organisation Plans | Tax breakup and gapless invoice numbers |
| Prices include 18% GST; Razorpay fee 2.36% | Assumptions | Store gross, tax and fee separately on every payment |
| Credits run out: Fast models stay available for 10 messages a day | Limit Behaviour | A small grace allowance in the limit check |

**Load is small.** Even if every one of the \~11,580 users in the Growing scenario hit an 80-message cap, that is under 1M messages a day, about 11 a second on average. A single PostgreSQL primary and one Valkey instance handle this with wide headroom; the design is about correctness, not scale.

## How big companies design billing

Mature billing systems (Stripe, Uber, the usage-billing vendors such as Orb, Metronome and Lago) all separate the same six concerns, because each changes at a different speed and fails in a different way.

| Concern | Question it answers | Industry practice | In GranthanAI |
| --- | --- | --- | --- |
| Catalog | What do we sell, at what price? | Prices are immutable versions; a change creates a new version. Razorpay plans work the same way: they cannot be edited, only replaced | `plans.yaml` in git, loaded into `plan_versions` on deploy |
| Entitlements | What may this user do right now? | Computed from plan + overrides, cached, served fast; kept separate from payment state | Effective plan per user, cached in Valkey |
| Metering | What did they use? | Append-only events with a unique ID, aggregated later, never updated | `usage_events` keyed on `request_message_id` |
| Rating | What is that usage worth? | Price looked up at the time of use; both cost and price stored | ₹ cost from `model_prices`, paise price from the plan version |
| Invoicing and collection | What do they owe, and did they pay? | The processor holds cards and mandates; the merchant keeps its own system of record | Razorpay collects; billing-service owns invoices and status |
| Ledger | Where did every rupee go? | Double-entry, immutable, balanced per transaction; corrections are new entries | `ledger_entries`; wallet balances are sums, never stored counters |

### Ten practices to copy

1. **Money in integer minor units.** Paise as `BIGINT`, never floats. Fractional AI costs in micro-rupees.
2. **Idempotency keys on every write.** The client sends a key; the server stores the result and returns it on retry. Stripe made this the standard for payment APIs.
3. **Reserve, then commit or release.** Like a card authorisation hold: reserve a message before generation, commit on success, release on failure.
4. **Webhooks are hints.** Verify the signature, store the raw event, dedupe on its ID, then fetch the object from the processor before changing state. Poll for anything stuck.
5. **Transactional outbox and inbox.** Write the state change and the event to publish in one database transaction; consumers record processed event IDs in the same transaction as their effect.
6. **Append-only history.** No `UPDATE` or `DELETE` on usage or ledger rows; a refund or correction is a reversing entry.
7. **Daily three-way reconciliation.** Processor vs your payments, your ledger vs itself, provider AI bills vs your rated usage.
8. **Grandfathering by reference.** A subscription points to the plan version it was sold on, so a price change never silently rewrites old customers.
9. **A predictable degraded mode.** When billing is down, cheap actions continue under a small local cap and expensive ones stop.
10. **Time is an input.** Renewals, trials and expiries are tested with a controllable clock, like Stripe's test clocks.

## Build vs adopt

Build. The open-source billing engines are good, but the hard part for GranthanAI is India-specific (Razorpay, UPI Autopay, GST, RBI e-mandate rules) and none of them covers it out of the box.

| Option | Licence | What it gives | Why not now |
| --- | --- | --- | --- |
| [Lago](https://dev.co/devops/open-source/lago) | AGPLv3 | Metering, plans, usage invoices; ClickHouse for event aggregation | Named payment integrations are Stripe, Adyen and GoCardless; no Razorpay found. You would still write the Razorpay and UPI glue, and run its stack plus ClickHouse beside your Python services |
| [Kill Bill](https://www.blog.brightcoding.dev/2026/08/06/killbillkillbill-open-source-subscription-billing-for-saas) | Apache 2.0 | Mature subscription engine (since 2010), payment plugins | Java/JVM; functionality spread over many repositories; a Razorpay plugin would be yours to write and maintain |
| [OpenMeter](https://konghq.com/company/press-room/press-release/kong-acquires-openmeter-to-unlock-ai-and-api-monetization-for-the-agentic-era) | Open source (check the repo's licence) | Usage metering for APIs and LLMs | Acquired by Kong on 3 Sep 2025 and being folded into Kong Konnect; roadmap risk, and you already have usage on Kafka |
| [Hyperswitch](https://finextra.com/pressarticle/104959/juspay-activates-open-source-payments-orchestration-platform) (Juspay) | Apache 2.0 | Payment orchestration: plug in several processors, routing | Not needed with one gateway. Revisit when you add a second gateway for failover; confirm its Razorpay support first |
| Thin billing-service in Python | Yours | Exactly your plans, Razorpay, UPI, GST, Valkey limits | You own correctness; the ten practices above are how you keep it |

Use Lago's and OpenMeter's public docs as design references: their event, meter and entitlement models are a good checklist for your own tables.

## Architecture

One codebase, two processes: an API pod on the request path and a worker pod for everything that can wait. billing-service owns its database; other services never read its tables, they call its API or consume its events.

&#91;embedded content: billing-service architecture · hot path in accent\]

conversation-service reserves before every message and llm-service releases on failure; both are the only calls a user waits on. Usage, invoices, alerts and reconciliation run in the worker from Kafka and the job table.

| Component | Runs as | Talks to |
| --- | --- | --- |
| billing-api | FastAPI + gRPC container, 2 replicas behind api-gateway | PostgreSQL, Valkey, Razorpay |
| billing-worker | Same image, worker command; jobs use `SKIP LOCKED`, so a second replica is safe | Kafka, PostgreSQL, object storage, Razorpay |
| Valkey | Dedicated instance, AOF every second | billing-service only |
| `billing` database | Own database and role on your Postgres server, point-in-time backups | billing-service only |
| Kafka topics | Consumes `chat.response.generated`, `org.member.joined`, `org.member.left`, `user.deleted`; publishes `subscription.changed`, `payment.failed`, `usage.threshold_reached`, keyed by billing account | All services |
| Public routes | `/billing/checkout`, `/billing/checkout/verify`, `/billing/me`, `/billing/cancel`, `/billing/webhooks/razorpay` (no JWT, signature-checked), `/admin/billing/*` (staff JWT) | Via api-gateway |

## Open-source stack: what, why and where

Keep the stack you already run and add nothing a solo founder cannot operate. Razorpay is the only closed component, and it sits behind a `PaymentGateway` interface so it can be swapped or doubled later.

| Layer | Choice (licence) | Why this | Where it runs | Rejected, and why |
| --- | --- | --- | --- | --- |
| Service code | Python 3.12, FastAPI, gRPC, uv (MIT / BSD / Apache 2.0) | Same toolchain as your 10 repos; billing is I/O-bound, so Python is not the bottleneck | `billing-service` API pod + `billing-worker` pod | Go or Java: faster, but a second toolchain for no gain; the hot path's work happens inside Valkey |
| System of record | PostgreSQL 17 (PostgreSQL Licence) | Multi-row ACID transactions (subscription + ledger + outbox together), `NUMERIC`, constraints, row locks, partitioning | Its own `billing` database and role on your Postgres server | Cassandra: no multi-row transactions, never for money. MongoDB: SSPL, not OSI open source |
| Counters and cache | [Valkey](https://betterstack.com/community/comparisons/redis-vs-valkey.md) 8 (BSD-3, Linux Foundation) | Atomic Lua scripts for check-and-increment, TTL keys, sub-millisecond | A dedicated instance with AOF on, separate from any eviction cache | Redis moved to source-available licences after 7.2; if you already run Redis, the same scripts work, but new installs should be Valkey |
| Events | Apache Kafka, KRaft mode (Apache 2.0) | Already in your stack; replayable log for usage; per-user ordering by key | Existing cluster; topics listed under Architecture | RabbitMQ: no replay, and you would run two brokers |
| Contracts | Protobuf + Buf (Apache 2.0) | You already use gRPC; Buf fails CI on breaking schema changes | `granthan-contracts` repo shared by all services | Untyped JSON events: silent breakage between services |
| Scheduled work | Postgres job table with `SELECT … FOR UPDATE SKIP LOCKED` workers | Renewal checks, expiries and reconciliation need durability, not a new cluster | `billing-worker` | Temporal (MIT) is the upgrade when org dunning and multi-day workflows grow; Celery adds a broker |
| Usage analytics | Postgres partitioned `usage_events` + daily rollups; ClickHouse (Apache 2.0) later | Enough for cost dashboards at your scale; ClickHouse when Postgres queries slow down | Same Postgres; later a ClickHouse fed from Kafka | ClickHouse on day one: another stateful system to back up |
| Invoice PDFs | WeasyPrint (BSD) | HTML template to PDF, in Python | `billing-worker` | Paid PDF APIs |
| Object storage | Your existing MinIO, or SeaweedFS (Apache 2.0) | Stores invoice PDFs and exports | Existing cluster | MinIO's community edition went into [maintenance mode, source-only](https://dev.to/alexandrev/minio-in-maintenance-mode-what-it-means-for-the-community-edition-oem-users-and-open-source-3o4j) in late 2025, so plan to build your own images or move |
| Secrets | OpenBao (MPL-2.0) or SOPS | Razorpay key and webhook secret never sit in `.env` files | Platform repo | HashiCorp Vault: no longer under an open-source licence |
| Observability | OpenTelemetry, Prometheus, Grafana, Loki, Tempo, Sentry | Matches the release plan's monitoring stack | `granthan-platform` | Hosted APM: cost |
| Tests | pytest, Hypothesis, Testcontainers, time-machine | Property tests prove ledger invariants; real Postgres and Valkey in CI; controllable clock | CI | Mocks for the database: miss the bugs that matter |
| Payments | Razorpay (closed; no open-source acquirer exists for UPI Autopay) | Cards, UPI Autopay, eMandate, payment links, invoices | External | Hyperswitch in front of it once a second gateway is worth adding |

## Data model

The release plan's six tables grow to about twenty, grouped by concern. Usage and ledger tables are insert-only; a database trigger rejects `UPDATE` and `DELETE` on them.

| Module | Table | Key columns | Notes |
| --- | --- | --- | --- |
| Catalog | `plans`, `plan_versions` | plan\_code, version, interval prices in paise, limits JSON, effective\_from, razorpay\_plan\_id | Loaded from `plans.yaml`; a new price is a new version |
| Catalog | `model_prices` | model, group, input/output US$ per 1M tokens, effective\_from, effective\_to | Handles the 1 Jan 2027 Gemini change without touching old usage |
| Catalog | `fx_rates` | date, usd\_inr | Cost is computed with the rate of the day |
| Accounts | `billing_accounts` | id, owner\_type (user / org), owner\_id, gstin, billing\_email, state\_code | Who pays; one per user and one per organisation |
| Subscriptions | `subscriptions` | billing\_account\_id, plan\_version\_id, interval, seats, status, gateway, gateway\_subscription\_id, current\_period\_end, cancel\_at\_period\_end, grace\_until | One active row per billing account |
| Subscriptions | `subscription_history` | subscription\_id, from\_status, to\_status, cause, at | Every state change, for support and audits |
| Entitlements | `plan_overrides` | owner, plan\_code, reason, expires\_at, granted\_by | Pilots, demo accounts, goodwill |
| Metering | `reservations` | id, user\_id, kind, group, units, status (held / committed / released / expired), expires\_at, idempotency\_key (unique) | Mirror of the Valkey holds, for recovery |
| Metering | `usage_events` | request\_message\_id (unique), user\_id, billing\_account\_id, model, group, input / output / cached tokens, billable\_units, cost\_micro\_inr, price\_paise, plan\_version\_id, occurred\_at | Partitioned by month |
| Metering | `usage_daily` | billing\_account\_id, user\_id, day, group, messages, cost, price | Rollup for dashboards and org reports |
| Money | `invoices`, `invoice_lines` | number, financial\_year, billing\_account\_id, period, taxable\_paise, cgst / sgst / igst\_paise, total\_paise, pdf\_key | One invoice per payment or per org billing period |
| Money | `invoice_counters` | financial\_year, series, next\_number | Locked row, not a Postgres `SEQUENCE` (sequences leave gaps on rollback) |
| Money | `payments`, `refunds` | gateway\_payment\_id, amount\_paise, gst\_paise, fee\_paise, status | Gross, tax and fee kept apart |
| Ledger | `ledger_accounts`, `ledger_transactions`, `ledger_entries` | account, transaction, signed amount\_paise | Entries in a transaction must sum to zero (deferred constraint) |
| Wallet (later) | `credit_lots` | wallet\_account\_id, amount\_paise, remaining\_paise, expires\_at | 12-month validity; spent oldest first |
| Plumbing | `webhook_inbox`, `processed_events`, `outbox`, `idempotency_keys` | gateway\_event\_id / event\_id (unique), payload, status | Exactly-once effects on top of at-least-once delivery |

### Money rules

- **Units.** Charges in paise (`BIGINT`). AI cost in micro-rupees, because one request-analysis call costs a fraction of a paisa (the workbook's overhead is ₹0.0285 a message). Keep the raw US$ cost and the FX rate used.
- **GST-inclusive prices.** Taxable value = round(gross × 100 / 118); tax = gross − taxable. Split CGST + SGST or IGST by the customer's state against your registered state. Confirm the rounding and place-of-supply rules with your chartered accountant.
- **Invoice numbers.** Consecutive and unique per financial year, reset each April. Allocate the number inside the same transaction that creates the invoice.
- **Time.** Store UTC. A "day" for daily limits is the Asia/Kolkata calendar day, so limits reset at midnight IST as the Limit Behaviour sheet promises.

### Example ledger postings

| Event | Debit | Credit | Paise |
| --- | --- | --- | --- |
| ₹499 Pay as you Go top-up captured | Razorpay clearing | Customer wallet (liability) 42,288; GST payable 7,612 | 49,900 |
| One Best message on Pay as you Go | Customer wallet | PAYG revenue | 339 |
| Unused credit expires after 12 months | Customer wallet | Breakage revenue | remaining |
| Razorpay settles, fee deducted | Bank; Payment fees | Razorpay clearing | net; fee |

GST on Pay as you Go credits is charged when they are bought, as an advance for services, so the wallet holds the taxable value. The ₹499 top-up puts ₹422.88 in the wallet and ₹76.12 in GST payable, and a ₹4 Best message takes ₹3.39 (₹4 × 100/118) from the wallet. The app shows balances GST-inclusive.

## Key flows

### 1. Every message: reserve, generate, commit

1. conversation-service calls `CheckAndReserve(user_id, kind=message, requested_group, key=request_message_id)` over gRPC before publishing `chat.message.created`.
2. billing-service reads the user's entitlement snapshot from Valkey. On a miss it resolves it from Postgres in this order: active `plan_override`, then the higher of the user's organisation plan and personal plan, then Free. The snapshot is cached and invalidated by `subscription.changed` and `org.member.*` events.
3. One Lua script, atomically: checks the daily total and the group counter against the limits; on Max, turns the 13th Best request into Smart; on Pay as you Go, checks available balance (balance minus open holds) and the spend cap; then increments the counters and writes a hold with a 10-minute TTL. It returns allow or deny, the group to use, the hold ID and a reason code that maps to the Limit Behaviour copy.
4. The message event carries the hold ID and group; llm-service uses that group and the plan's reading cap.
5. llm-service publishes `chat.response.generated` with provider-reported token counts (not streamed chunk counts), the model and the hold ID.
6. The billing usage consumer, in one Postgres transaction: dedupes the event, inserts the `usage_event` rated at `occurred_at`, commits the hold, posts the wallet debit on Pay as you Go (units = ceil(input tokens / 32,000) × group price), and writes 80% / 100% threshold events to the outbox.
7. If generation fails, llm-service calls `Release` and the user is not charged. A hold that expires with no event gives the message back; if the usage arrives later, it is counted and billed then. Rising expiries raise an alert.

The atomic script is what stops two browser tabs both passing at 59 of 60. If billing-service is unreachable, conversation-service's circuit breaker allows Fast models only, up to 10 messages per user per day (the same allowance as "credits run out"), and refuses Best and Ultra. Target: `CheckAndReserve` under 10 ms at p99.

### 2. Individual checkout

1. `POST /billing/checkout {plan, interval}` with an `Idempotency-Key`. billing-service creates a `pending` subscription and the Razorpay subscription for that plan version.
2. The user authorises a card or UPI Autopay mandate in Razorpay Checkout; the first payment needs two-factor authentication.
3. The frontend posts the checkout response to `/billing/checkout/verify`; the server checks Razorpay's signature and activates.
4. The `subscription.activated` and `subscription.charged` webhooks arrive and are applied idempotently: invoice, ledger entries, `subscription.changed` event.
5. A reconciler polls Razorpay for any subscription still `pending` after 15 minutes.

Razorpay shows UPI at checkout only when the amount is under ₹15,000, so Max yearly at ₹14,990 just fits. Any yearly price at or above ₹15,000 loses UPI.

### 3. Renewal, failed payment and downgrade

The state machine is drawn below. Razorpay and the bank send the RBI-required pre-debit notice at least 24 hours before each charge; notification-service adds a "renewing tomorrow" email. On a failed charge the subscription goes `past_due` with a 3-day grace period and full access. If Razorpay halts the subscription or grace ends, entitlements fall to Free. Nothing is deleted: content over the Free limits becomes read-only until the user pays or removes it.

&#91;embedded content: subscription state machine · 5 states\]

### 4. Changing plan

Razorpay lets you update only card-authorised subscriptions; UPI and eMandate subscriptions cannot be updated, so use one method for all:

- **Upgrade:** one checkout for a new mandate at the new plan's full price, starting now. The period restarts that day and the unused days of the old plan are refunded with a credit note; entitlements switch on the first charge. This avoids depending on Razorpay add-ons with a future start date, which its docs do not describe.
- **Downgrade:** create the new mandate to start at the period end and cancel the old one at cycle end. Entitlements change at the boundary.

### 5. Organisation seats

- Yearly seat bills exceed the ₹15,000 frictionless autopay limit (50 college seats × ₹249 × 12 = ₹1,49,400), so organisations pay by GST invoice with a Razorpay payment link or bank transfer. A webhook or a manual "mark paid" in admin-console activates the plan.
- org-service enforces seats; billing-service records seats bought. Members added mid-term are billed on a prorated quarterly true-up invoice.
- Usage is attributed to the billing account whose plan granted it, so org reports and org margins stay accurate.

### 6. Pay as you Go wallet (Phase 3)

- Top-up: one-time Razorpay order; on `payment.captured`, a credit lot with a 12-month expiry, and a tax invoice for the top-up.
- Use: credits pay for Pay as you Go users' messages, and for a subscriber's messages once their plan denies one (daily limit, or a model the plan lacks). Debits come from the oldest lot first; open holds reduce the available balance.
- Spend cap: month-to-date debits are checked in the same Lua script; 80% and 100% raise events.
- Expiry: a nightly job moves expired remainders to breakage revenue, after a reminder email 30 days ahead.
- Organisation wallet: one shared wallet account, with the per-member cap from org policies.
- GST is charged at top-up; your chartered accountant signs this off before launch.

## Reliability, security and compliance

Each known way billing goes wrong has one named control.

| Risk | Control |
| --- | --- |
| Double charge from a retried request | `Idempotency-Key` on every mutating API; unique constraints on gateway payment and event IDs |
| Webhook duplicated, late or out of order | Inbox dedupe on event ID; fetch the subscription from Razorpay before acting; the state machine ignores backward moves |
| Kafka event lost or stuck | Outbox on the producer, inbox on the consumer; consumer-lag alert; hold expiry as a backstop |
| Valkey lost | AOF persistence; today's counters rebuilt from `usage_events` plus open `reservations` |
| billing-service down | Degraded mode: Fast models only, 10 a day, Best and Ultra refused |
| Free-tier abuse | Verified email, per-IP signup and message limits at api-gateway |
| Card or UPI data exposure | Razorpay Checkout collects payment details; your servers never see them |
| Forged webhook | HMAC-SHA256 signature check with the secret from OpenBao; raw body stored before parsing |
| Silent edits to money | App database role cannot `UPDATE` or `DELETE` ledger or usage rows; admin actions audited with reason |

### India rules that shape the design

- **RBI e-mandate framework (2026).** After a one-time authenticated mandate, recurring debits up to ₹15,000 run without OTP; higher debits need authentication each time. Customers get a pre-debit notice at least 24 hours ahead with merchant, amount and date, and can pause, change or cancel mandates at any time ([Business Today, 21 Apr 2026](https://www.businesstoday.in/personal-finance/news/story/rbi-caps-recurring-payments-at-rs15000-without-otp-under-new-e-mandate-framework-526759-2026-04-21)). Treat customer-side cancellation as a normal event, not an error.
- **Razorpay specifics.** UPI and eMandate subscriptions cannot be updated; plans cannot be edited; UPI customers can cancel from their UPI app; UPI debits can land on T+1 because of retries ([Razorpay Subscriptions FAQ](https://razorpay.com/docs/payments/subscriptions/faqs.md)).
- **GST.** Invoice per payment, consecutive numbering per financial year, organisation GSTIN on the invoice, records kept for the statutory period. Confirm with your chartered accountant.
- **Gateway activation.** Terms, privacy, refund and cancellation pages must be live before Razorpay activates the account (release plan, section 8).

### Daily reconciliation

1. Razorpay payments and settlements vs `payments` and the Razorpay clearing account.
2. Ledger: every transaction sums to zero; wallet balances equal the sum of their entries.
3. AI cost: each provider's usage export vs the sum of `cost_micro_inr` per provider per day.
4. Valkey counters vs `usage_events` for yesterday.

A mismatch opens an alert and a row in admin-console; nothing is auto-corrected.

### What to watch

- `CheckAndReserve` latency, deny rate by reason, open holds, hold expiries.
- Webhook delivery lag and failures; payment failure rate.
- Reconciliation differences by check.
- AI cost per active user by plan against the workbook's averages (Pro ₹121, Max ₹434 a month), plus the top 20 users by cost.

## Build order

The phases match the release plan; each ends with something people can pay for.

**v1, paid individual launch (Free, Pro, Max)**

1. Contracts: Protobuf for `CheckAndReserve`, `Release` and the billing events.
2. Catalog loader from `plans.yaml`; `model_prices` and `fx_rates`.
3. Billing accounts, subscriptions, Razorpay checkout for cards and UPI Autopay, webhook inbox, reconciler.
4. Entitlement resolver and cache; `CheckAndReserve` / `Release` with the Lua script and degraded mode.
5. Usage consumer with rating; requires llm-service to send real token counts.
6. Invoices with GST breakup and numbering; payments ledger; PDFs.
7. `/billing/me` for the usage meter; `/admin/billing/*` for overrides and cost; daily reconciliation and alerts.

**v2, organisation pilots:** organisation billing accounts, seat subscriptions, invoices with payment links, quarterly true-ups, usage-by-member reports for org-service.

**v3, Pay as you Go and scale:** credit lots, wallets, spend caps, organisation wallets; then ClickHouse, Temporal or Hyperswitch only when a measured need appears.

## Open decisions

Settled on 1 Nov 2026; the reasoning is in the Decisions section of GranthanAI billing-service LLD.

- [x] Plan precedence for org members: the higher plan wins, the org's on a tie. When the org plan covers a member's own plan, they are told they can cancel; billing never cancels it for them.
- [x] Grace period after a failed renewal: 3 days, then `suspended` (Free limits) while Razorpay keeps retrying; a late successful charge restores the plan.
- [x] Expired holds: the message is given back; if the usage arrives late it is counted and billed then.
- [x] Pay as you Go credits work on top of any plan: a subscriber's plan is used first, and credits pay only for what it denies.
- [x] GST: state required at checkout; invoices in paise; GST charged when credits are bought; SAC code and GST registration are settings. Your chartered accountant signs off before the first paid invoice.
- [ ] Keep MinIO with self-built images, or move object storage to SeaweedFS.

## Sources

- GranthanAI subscription plans workbook and release plan (30 Oct 2026)
- [RBI e-mandate framework coverage, Business Today, 21 Apr 2026](https://www.businesstoday.in/personal-finance/news/story/rbi-caps-recurring-payments-at-rs15000-without-otp-under-new-e-mandate-framework-526759-2026-04-21)
- [RBI auto-debit rules explained, Business Today, 2 May 2026](https://www.businesstoday.in/personal-finance/news/story/rbi-auto-debit-rules-explained-what-new-changes-mean-for-your-upi-and-card-payments-528507-2026-05-02)
- [Razorpay Subscriptions FAQ](https://razorpay.com/docs/payments/subscriptions/faqs.md) and [supported payment methods](https://razorpay.com/docs/payments/subscriptions/supported-payment-methods.md)
- [Lago overview, dev.co](https://dev.co/devops/open-source/lago)
- [Kill Bill overview, Bright Coding, Aug 2026](https://www.blog.brightcoding.dev/2026/08/06/killbillkillbill-open-source-subscription-billing-for-saas)
- [Kong acquires OpenMeter, 3 Sep 2025](https://konghq.com/company/press-room/press-release/kong-acquires-openmeter-to-unlock-ai-and-api-monetization-for-the-agentic-era)
- [Hyperswitch launch, Finextra](https://finextra.com/pressarticle/104959/juspay-activates-open-source-payments-orchestration-platform)
- [Redis vs Valkey, Better Stack](https://betterstack.com/community/comparisons/redis-vs-valkey.md)
- [MinIO in maintenance mode, DEV Community](https://dev.to/alexandrev/minio-in-maintenance-mode-what-it-means-for-the-community-edition-oem-users-and-open-source-3o4j)
