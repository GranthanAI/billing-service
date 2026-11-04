# Stage C: Phase 7, usage metering and rating

## What changed
- **Migration `0013_nullable_billing_email`.** `billing_accounts.billing_email` is nullable.
  Checkout still requires an email; a Free user's account is created lazily without one
  (`AccountRepo.get_or_create(owner_type, owner_id, email=None)`).
- **`app/usage.py` (`UsageService.apply`).** One event, in order: claim `processed_events`, insert
  `usage_events` (one transaction), then after the commit settle the hold in Valkey and raise
  threshold events.
  - *Attribution.* With a hold (`peek_hold`): its account and plan version. Without one (expired,
    or a degraded-mode message with `hold_id` null): resolve the entitlement now and bill the plan.
    An empty account gets `get_or_create` with no email; `subscription_id` comes from the
    account's live subscription.
  - *Status.* `success`: rate, insert, `commit.lua`. `error`: release the hold, insert the cost of
    all calls with units 0 and price 0. `cancelled`: as `error` if every call has zero output
    tokens, else as `success`.
  - *Rating.* The Phase 1 `rate`, with prices and FX at the event's `occurred_at`.
    Manim = calls + `MANIM_RENDER_COST_USD` (0.01) at FX, units 1. YouTube =
    `YOUTUBE_INGEST_COST_INR` (0.30) in micro-rupees, units 1. Unknown model price raises, so the
    consumer retries and dead-letters it.
  - *Rows.* `occurred_at` is from the event; tokens from the answer call; `aux_*` is the sum of
    the other calls.
- **Consumer `billing-usage`** (`adapters/kafka/usage_consumer.py`) on `chat.response.generated`,
  `chat.response.cancelled`, `artifact.manim.rendered`, `file.youtube.ingested`, DLQ
  `billing.usage.dlq`. The consumer's decoder now receives the topic; a malformed body goes
  straight to the DLQ, a failing handler after 5 tries. New topic `chat.response.cancelled`
  (topics list, compose).
- **Threshold events.** After a successful commit: 80% and 100% of `messages_per_day`,
  `best_per_day` and `youtube_per_month`, guarded by `SET NX thr:{uid}:<limit>:<period>:<percent>`
  with a TTL to the end of the period, then `usage.threshold_reached` in the outbox (new small
  unit of work).
- **`usage.rollup`** (daily 00:30 IST, yesterday, or `payload.day`).
- **`python -m billing.cli dlq replay --topic <dlq> [--limit N]`.** Republishes each dead letter to
  its `source_topic` header without the error headers.
- **Metrics:** `billing_usage_events_total{kind,result}` (applied, duplicate, dlq),
  `billing_usage_cost_micro_inr_total{provider,model_group,plan}`,
  `billing_usage_late_commit_total`.

## Deviations and choices the docs did not spell out
- **Several `answer` calls** (a retry inside llm-service): the last one sets the units and the
  primary tokens; earlier answers and every other call add to the cost and to `aux_*`. Unknown
  purposes are cost-only too, so a new llm-service purpose never dead-letters a message.
- **A replay still settles the hold.** A duplicate event skips the insert but still runs
  `commit.lua`/release, whose own done marker makes that safe. This heals a crash between the
  database commit and the Valkey commit, which the LLD only covers with the wallet resync.
- **Events with no hold** (degraded mode) are committed to the day counters like a late event
  (`commit.lua` with no hold counts it) and the late-commit metric rises.
- **Failed attempts are stored** with units 0, so the counters rebuild, `usage_daily.messages`
  and the YouTube month count now ignore rows with `billable_units = 0`; costs still include them.
- **`usage.rollup` upserts**, not delete + insert: the app role has no `DELETE` on `usage_daily`
  (Phase 2 grants). The source rows are append-only, so recomputing in place gives the same rows.
  It would not remove a group row whose events vanished, which cannot happen.
- **A lost alert is possible** if the process dies between the `SET NX` guard and the outbox write
  (accepted by the prompt).
- **DLQ replay stops at the DLQ's end offsets as they were at the start.** The first version
  looped forever: a message that failed again was dead-lettered again and the running replay
  re-read it. Found by the end-to-end test.
- **`make_valkey`** (Stage B) is a bounded pool; the usage consumer uses the same client.
- The `UsageService` takes a neutral `UsageEvent`; `adapters/kafka/usage_consumer.py` converts the
  JSON schemas, so `app` still imports no adapter.

## Tests
- `tests/integration/test_usage.py` (14): an event replayed three times leaves one row and one
  commit; the workbook message is exactly 118,750 micro-INR; a late event after the sweeper
  expired the hold is counted again and billed to the plan; no hold; error releases and records
  cost at price 0; cancelled with and without output; a retried answer; unknown purpose; a Free
  user's account without an email; Manim includes the render; the 4th Free YouTube video is denied
  at reserve and an allowed one is rated; thresholds fire once at 80% and once at 100%; replaying
  a recorded day twice gives identical `usage_daily`; the rollup defaults to yesterday in IST.
- `tests/integration/test_usage_consumer.py` (2): JSON in, good event once despite a duplicate,
  malformed JSON and an unknown model dead-lettered, price added, replay succeeds; replay limit,
  header stripping, no repeat.
- `tests/unit/test_usage_unit.py` (7): status rule, call normalisation, cost helpers, event
  conversion, malformed input.

## Environment
Nothing needed beyond Stage B. Docker containers on the machine were left alone (the retrieval
service is being tested on them).

## For later stages
- Stage D: `NextMandateFailed` etc. do not touch usage. `housekeeping` must keep `processed_events`
  for at least the Kafka retention window; 30 days as in the LLD is fine.
- Stage E: `usage_daily` rows exist for `GET /me/usage`; the margin report can use
  `usage_events.cost_micro_inr` per provider (the label on the cost metric is the provider name
  as llm-service reports it).
- Stage G: `rate(..., mode="payg", ...)` is ready; `UsageService` passes plan mode and no PAYG
  prices today, so the credits branch is a small addition in `_record` and `_settle`.

## Exit check
- `make lint`: clean.
- `make test`: **382 passed, twice in a row** (399 s and 467 s); domain coverage 99.5%.
- Replaying a recorded day of events twice gives identical `usage_daily`
  (`test_replaying_a_recorded_day_twice_gives_identical_usage_daily`).
- Load gate: skipped by instruction (launch blocker in `runbooks/v1-launch.md`).
