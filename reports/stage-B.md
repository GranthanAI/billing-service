# Stage B: Phase 5b, model catalog, Auto, picker endpoints and JSON events

## What changed
- **Kafka events are JSON.** `adapters/kafka/schemas.py` (Pydantic v2). Produced events
  (`subscription.changed`, `payment.failed`, `usage.threshold_reached`, `invoice.issued`) are
  `extra="forbid"` with `event_type`, `schema_version` "1.0", `event_id` (UUIDv7), `occurred_at`
  (ISO 8601 UTC, `...Z`), `correlation_id`, `producer` at the top, then their own fields. Topics,
  keys and headers are unchanged. Consumed shapes (`chat.response.generated` 2.2,
  `chat.response.cancelled`, `artifact.manim.rendered`, `file.youtube.ingested`, `user.deleted`)
  are `extra="ignore"`, with `dedupe_key` (`chat.response.generated:<request_message_id>` and the
  cancelled twin; the others use their `event_id`) and `decode_consumed(topic, bytes)` for Stage C.
- **Protos.** `proto/granthan/events/` and its generated code are deleted. `billing.proto` is the
  only proto left, so `buf breaking` against main compares just that file.
- **Catalog (migration `0012_catalog_models`).** `plans.yaml` `model_groups` is now a list of
  `{id, label, provider}` per group (the 12 models, in the order given). `default_group` on every
  plan (free fast; pro, max, payg and the org plans smart); validated to be one of the plan's
  groups and never best/ultra. It is stored on `plan_versions`, on the domain `PlanVersion`, in the
  entitlement cache, and in the checksum. New `models` table (app role read-only, like
  `plan_versions`), upserted by `billing catalog sync`; a label or order change updates it without a
  new plan version.
- **Auto.** `requested_group` unspecified with an empty `requested_model` reserves the plan's
  `default_group`. `CheckAndReserveResponse.granted_model = 12` is set only for an explicit model.
  Auto cannot reserve Best because the catalog rejects a best/ultra default.
- **`GET /billing/v1/models`** (public, `ETag`, `Cache-Control: public, max-age=300`, 304) and
  **`GET /billing/v1/me/models`** (`X-User-Id`, 401 without it). Implemented by
  `app/model_picker.py`; the plan and model lists are cached in process for 60 s, the entitlement
  comes from the entitlement cache and today's counts are one Valkey read, so the endpoint does not
  touch Postgres after warm-up.
- **`make proto-check`:** `buf lint` and `buf breaking` against main (local `buf` if present,
  otherwise the official Docker image). `.github/workflows/` is untouched.

## Tests
- New: `tests/unit/test_kafka_schemas.py` (JSON shape, extra fields, dedupe keys, malformed
  bodies), catalog loader rules (labels, `default_group`, checksum behaviour), catalog sync
  (default groups and models stored, label-only change, removed model, default-group change is a
  new version), Auto for Max/Pro (Best counter never touched, `granted_model`), Auto over gRPC,
  `/models` ETag/304, `/me/models` for Free, Max (Best 12 then 11), an unknown user, PAYG on/off
  and no header. The cross-replica invalidation test is from Stage A.
- Changed: the codec/outbox/exactly-once tests now run on JSON (behaviour identical); two tests
  that expected "no group" to be refused now expect Auto.
- Exit: see the bottom of this file for the lint and two full runs.

## Deviations and choices the docs did not spell out
- **`default_group` joins the checksum, so every plan gets a new version on the first sync**
  (reported as "limits only" because prices are equal). The sync writes one `admin_audit` row per
  plan; Stage E's "apply limits" endpoint is how live subscribers would be moved. Live subscribers
  keep their old version until then, which is why migration 0012 backfills the old rows (free fast,
  others smart). With real Razorpay, the new checksum also creates new Razorpay plans for Pro and
  Max (plans cannot be edited); the old ones keep serving existing subscribers.
- **Picker order** is group order (fast, smart, best, ultra), then the order in the file. The
  smart group is listed in the order of your table, which differs from the previous file.
- **`/me/models` numbers.** A group that is not allowed reports `daily_limit: 0` and
  `remaining_today: 0`; allowed non-Best groups share the daily total; Best reports
  `min(best remaining, daily remaining)`. Unlimited is `null`.
- **`upgrade_to`** is the lowest-ranked individual plan whose groups include the group (so Free's
  Smart and Best point to `pro` and `max`); Ultra is `payg` only when `PAYG_ENABLED`.
- **Old cache entries** (written before `default_group` existed) decode with Smart if the plan has
  it, else Fast, so deploying does not need a cache flush.
- **`buf breaking`** is a plain run against main: with the event protos gone from main there is
  nothing else to filter out. The `--path` filter the prompt asked for does not work across a
  module boundary in buf v2 (the base and the input name files differently).
- A `models` row is removed when it leaves the file (the picker list follows the file); its price
  row stays.

## Environment note
`make catalog-sync` was run against the local dev database (migrated to 0012): first run created
v2 of every plan and 12 models, second run "unchanged, 0 models changed".

## For later stages
- Stage C uses `decode_consumed` and the `dedupe_key`s above; `LlmCall.usage_estimated` is read but
  not used yet.
- Stage E: the "apply limits to existing subscribers" endpoint now has real audit rows waiting
  (one per plan from this change).

## Exit check
- `make lint`: clean (ruff, mypy --strict, import-linter).
- `make test`: **359 passed, twice in a row** (349 s and 365 s); domain coverage 99.5%.
- `make proto-check`: `buf lint` and `buf breaking` pass.
- `make catalog-sync` twice on the dev database: second run unchanged, 0 models changed.
- One more fix made on the way: `make_valkey` now uses a bounded blocking pool (64 connections).
  The first full run failed `test_200_concurrent_reserves_allow_exactly_the_limit` because 200
  concurrent reserves each opened a new connection and the burst timed out in the handshake on
  this loaded machine. This is also the "Valkey pool size against concurrency" item from Stage A.
- Load gate: skipped by instruction; it is a launch blocker in `runbooks/v1-launch.md`.
