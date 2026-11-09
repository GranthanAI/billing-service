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

## Integration plan, phase P1 additions

Source: `docs/Integrations/GranthanAI_Billing_Integration_Plan.md` sections 4.1 to 4.8. The plan wins
where it disagrees with this file.

### `chat.message.created` 2.4: the `billing` block

Add this optional block (all fields additive; llm-service falls back to its legacy routing when the
block is absent or `granted_group` is null):

```json
"billing": {
  "hold_id": "0193...", "requested_model": null, "granted_group": "best",
  "granted_model": null, "max_context_tokens": 128000, "funded_by": "plan",
  "downgraded": false, "enforcement": "enforce"
}
```

`hold_id` is null in degraded mode or after a shadow-mode deny; `requested_model` null means Auto;
`funded_by` is `plan` or `credits`; `enforcement` is `enforce`, `shadow` or `degraded`.

### Idempotency keys

Chat message and regenerate: the reserve `idempotency_key` and the usage `source_id` are both the
`request_message_id` (a new UUIDv7 per send; a regenerate gets a new one).

### Order inside one request

Service-owned limits first (no side effects), then `CheckAndReserve` (spends a count), then the work.
A failure after the reserve is compensated with `Release`. Service-owned limits (plan 4.8):
`messages_per_conversation`, `branches_per_tree`, `documents_per_tree`, `custom_modes`,
`mode_instruction_chars`; a breach is `403` with `reason: plan_limit`
and `message_key: limit.<name>` (for example `limit.branches_per_tree`), and nothing is reserved.

### Deny error shape

```json
{"detail": {"code": "billing_denied", "reason": "daily_limit", "message_key": "limit.daily",
            "message": "Daily limit reached. Resets at midnight, or upgrade / top up.",
            "resets_at": "2026-11-09T00:00:00+05:30", "upgrade_to": "max"}}
```

| reason | HTTP | message_key |
| --- | --- | --- |
| `daily_limit` | 429 | `limit.daily` |
| `monthly_limit` | 429 | `limit.monthly` |
| `model_not_in_plan` | 403 | `plan.model_not_included` |
| `account_blocked` | 403 | `account.blocked` |
| `no_credit` | 402 | `credits.empty` |
| `spend_cap` | 402 | `credits.spend_cap` |
| `member_spend_cap` | 402 | `credits.member_cap` |
| `billing_unavailable` | 503 | `billing.degraded` |
| `plan_limit` | 403 | `limit.<name>` |

An allowed-but-downgraded answer carries `notice_key: "limit.best_switched"`.

### Entitlements cache

Call `GetEntitlements` and cache per user for 60 s. Consume the new topic **`entitlements.changed`**
(and `user.deleted`) and drop that user's entry. Billing unreachable: use the cached value for up to
1 hour, then Free limits. `-1` means unlimited in every limit. Never delete data when limits shrink:
make it read-only.

#### `entitlements.changed` 1.0 (billing produces it, key `user_id`)

```json
{"event_type": "entitlements.changed", "schema_version": "1.0", "event_id": "...",
 "occurred_at": "2026-11-08T10:00:00Z", "producer": "billing-service", "user_id": "...",
 "reason": "subscription|override|block|org|wallet|catalog", "version_hash": "sha256:..."}
```

Sent through billing's outbox in the transaction of the change, for every user whose entitlement may
have changed; an organisation change sends one event per member (a short moment later, from a job,
because org-service is asked for the member list). `version_hash` is an opaque marker that differs
for every announcement; do not compare it with anything.

### Usage topics billing reads

`chat.response.generated` 2.2 and `chat.response.cancelled` are produced by llm-service (see
`contracts/llm-service.md`); conversation-service only stores `model` and `model_group` from them.

### `usage.threshold_reached`

Consume it to push a `notice` SSE event to the user's open stream (80% and 100% of daily messages,
Best and YouTube).
