# What conversation-service must do with billing-service

billing-service exposes `granthan.billing.v1.BillingInternal` on gRPC port 50051 (inside the
cluster network only). The proto is `proto/granthan/billing/v1/billing.proto`.

## Authentication

Every call carries `authorization: Bearer <service token>` in metadata. billing accepts any token
in its `SERVICE_TOKEN_KEYS` list (comma-separated), so tokens can rotate: add the new one to
billing, switch callers, then remove the old one. Send `x-correlation-id` too; billing copies it
into logs and into the events it publishes.

`user_id` must be the id from the verified JWT (api-gateway's `X-User-Id`). billing never trusts a
user id taken from a client payload.

## New deny reason: the organisation's per-member limit (Pay as you Go)

| `message_key` | `deny_reason` (proto `DenyReason`) | HTTP status to the client |
| --- | --- | --- |
| `credits.member_cap` | `DENY_REASON_MEMBER_SPEND_CAP = 7` (additive) | **402** |

Shown when a member of an organisation whose wallet pays for their overflow has used their monthly
limit (`member_spend_cap_paise` in org-service, per member per IST month). Copy:
"Your organisation's monthly limit for you has been reached. Ask your organisation admin to raise
it." Other members of the same organisation can still spend. A deny is not stored; the limit
resets on the 1st of the next IST month. Clients that do not know value 7 should treat it like
`credits.spend_cap`.

## Call order for one message

1. **Reserve first.** Before publishing `chat.message.created`, call
   `CheckAndReserve(user_id, idempotency_key = request_message_id, kind = MESSAGE,
   requested_group, requested_model)`.
   Set `requested_model` when the user picked a specific model; otherwise set `requested_group`.
   Retrying with the same `idempotency_key` returns the same answer and the same `hold_id`, and
   never counts twice.
2. **If `allowed = false`**, do not publish. Show the copy for `message_key`:
   `limit.daily`, `limit.monthly`, `plan.model_not_included`, `credits.empty`,
   `credits.spend_cap`, `credits.member_cap`, `account.blocked`. `deny_reason` carries the same
   information as an enum.
   A deny is not stored, so retrying after the user upgrades succeeds.
3. **If `allowed = true`**, publish `chat.message.created` carrying:
   - `hold_id` (llm-service copies it into `chat.response.generated`, and uses it to `Release`);
   - the **granted** group (`granted_group`), not the requested one: a Max user's 13th Best
     message comes back as `SMART` with `downgraded = true` and
     `message_key = "limit.best_switched"`;
   - `max_context_tokens` (the plan's reading cap).
4. llm-service generates and publishes `chat.response.generated`. billing turns that into usage
   and commits the hold. conversation-service does nothing more for a successful message.
5. **If generation fails before any token is produced**, call `Release(user_id, hold_id,
   reason)` (`provider_error`, `cancelled` or `timeout`). It returns `released = false` when the
   hold is already gone, which is fine. If `Release` cannot be delivered, do nothing more: the
   hold expires after 10 minutes and the message is given back anyway.

The same flow applies to YouTube videos (`kind = YOUTUBE_VIDEO`, `idempotency_key` = video id) and
Manim renders (`kind = MANIM_VIDEO`); they need no model or group.

## Deadlines and retries

| Call | Deadline | Retries |
| --- | --- | --- |
| `CheckAndReserve` | 150 ms | none inside the deadline; the idempotency key makes a later retry safe |
| `Release` | 500 ms | 3 |
| `GetEntitlements` | 300 ms | cache the last answer for up to 1 hour; use Free limits when there is none |

billing returns `DEADLINE_EXCEEDED` at once, without touching its store, when less than 5 ms of
the deadline is left. Set deadlines on every call.

## When billing is unavailable (circuit breaker open)

Open the breaker after repeated `UNAVAILABLE` or `DEADLINE_EXCEEDED` on `CheckAndReserve`. While
it is open, conversation-service decides on its own:

- allow **Fast** models only, up to **10 messages per user per day**, counted in conversation-
  service's own Valkey (key per user per IST day, expiring after 48 hours);
- **deny Best and Ultra** (and anything that is not Fast) with the copy for `plan.model_not_included`;
- do not call `Release` for degraded messages: they hold nothing in billing.

Close the breaker after a successful probe. Messages sent in degraded mode are still billed
through `chat.response.generated` (they carry no `hold_id`), so billing counts them late.

## Errors

| gRPC status | Meaning | What to do |
| --- | --- | --- |
| `UNAUTHENTICATED` | missing or unknown service token | fix configuration; do not retry |
| `INVALID_ARGUMENT` | bad `user_id`, missing `kind`, `idempotency_key`, or group/model | fix the caller |
| `DEADLINE_EXCEEDED` | deadline too close, or billing too slow | count towards the breaker |
| `UNAVAILABLE` | billing down | count towards the breaker |
