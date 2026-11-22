# What llm-service must do with billing-service

> **Local dev address:** the shared dev instance (`scripts/dev_up.sh`) serves REST on `http://127.0.0.1:8090` and gRPC on `127.0.0.1:50061` (not 50051: memory-service owns that port). Always `127.0.0.1`, never `localhost` (Windows resolves it to IPv6 first). The service tokens and the gateway secret are printed by `dev_up.sh` and kept in `.env.dev`.


Source: `docs/Integrations/GranthanAI_Billing_Integration_Plan.md` sections 4.2, 4.4 and 4.9 (phase
P5). The plan wins where it disagrees with this file. llm-service never calls billing for chat; the
grant arrives in the event and the usage goes back in an event.

## In: `chat.message.created` 2.4, the `billing` block

See `contracts/conversation-service.md`. Use `granted_group` and `granted_model` (never the
requested ones), never escalate to a better group, and read at most `max_context_tokens`; **`max_context_tokens` 0 (or absent, or negative) means billing sets no cap**: use the model's own window and your own budget (Pay as you Go has unlimited reading). With no
block, or `granted_group` null, use the legacy routing unchanged.

- `granted_model` set: try that model first, then the rest of **its group**.
- `allowed_models` present (a list of catalog ids the plan allows inside `granted_group`): the chain is `granted_model` (if
  it is in the list), then the group's order **filtered to the list**; never any other model, even one of the same group.
  Empty after filtering: publish an error event with the calls so far (billing releases the hold). Absent: the whole group.
- `granted_model` null (Auto): the group's order (Fast: gpt-oss-20b, gemini-2.5-flash-lite; Smart:
  gpt-oss-120b, claude-haiku-5-5, gpt-6-luna, gemini-3.1-flash-lite, gemini-2.5-flash; Best:
  claude-sonnet-5-5, gpt-6.1-sol; Ultra: claude-opus-5-5; gpt-6-astra is not offered and ships disabled).
- `gemini-3.1-pro` is not in the catalog (preview only at Google); it comes back in Best when Google lists a GA id.
- Same-group fallback only. If the whole group fails, publish an error event with the calls so far.
- Models are named by the catalog IDs of `GET /billing/v1/models`, never by provider strings, in
  events and APIs. Models with no price in billing stay legacy-only.

## Provided: `GET /internal/models/availability`

Header `X-Service-Token` (setting `INTERNAL_SERVICE_TOKEN`; empty = the route refuses). Not routed by the gateway.
Answers `{"models":[{"id","available","reason"}],"checked_at"}` for every registry model: available = enabled AND provider
credentials present AND an adapter AND the breaker not open; `reason` is `disabled | no_credentials | no_adapter |
breaker_open` (empty when available). billing-service polls it every 5 minutes (plan 4.6.1).

## Out: `chat.response.generated` 2.3 and `chat.response.cancelled`

2.3 adds `"think": bool`: the answer call used provider reasoning. billing reads it and stores nothing; a think answer is
rated on its real tokens like any other.

Added fields (all optional for old consumers):

```json
"hold_id": "0193...", "model_group": "best", "model": "claude-sonnet-5-5", "downgraded": false,
"calls": [
  {"provider": "anthropic", "model": "claude-sonnet-5-5", "purpose": "answer",
   "input_tokens": 6000, "cached_input_tokens": 0, "output_tokens": 700, "usage_estimated": false},
  {"provider": "groq", "model": "openai/gpt-oss-20b", "purpose": "request_analysis",
   "input_tokens": 2000, "cached_input_tokens": 0, "output_tokens": 250, "usage_estimated": false}
]
```

- `model` is the catalog ID that actually answered, after any fallback inside the group.
- `purpose` is one of `answer`, `request_analysis`, `query_rewrite`, `workflow`, `research`,
  `manim_plan`. **Every** model call is listed: the analyzer, rewrite, workflow and research steps
  too. Token counts are the provider's own; when a provider reports none, estimate, set
  `usage_estimated: true` and count it.
- On error: `status = "error"` and `calls` lists every call made so far. Billing releases the hold
  and records the cost.
- On cancel: if every call has `output_tokens == 0` billing releases the hold; otherwise the message
  counts as used.
- `request_message_id` is the idempotency key billing deduplicates on. Pass it through in
  `memory.update.requested` too, so memory costs can be tied to the message.

## Manim renders

In `create_job`, after the per-user limits: `CheckAndReserve(kind = MANIM_VIDEO,
idempotency_key = "{job_id}:{attempt}")`. A deny returns the billing deny shape (429, 403 or 402)
with nothing enqueued. Store `hold_id` and `attempt` on the job; a retry increments `attempt` and
reserves again. Billing unreachable: allow the render with `hold_id = null` (renders are unlimited
on plans). On a final state publish one event per attempt:

```json
{"event_type": "artifact.manim.rendered", "schema_version": "1.0", "event_id": "...",
 "occurred_at": "...", "user_id": "...", "job_id": "...", "attempt": 1,
 "hold_id": "... or null", "status": "success|error", "calls": []}
```

Billing deduplicates usage on `(kind, source_id)` with `source_id = "{job_id}:{attempt}"` (attempt
defaults to 1 when absent), so a retry that succeeds after a failed first try **is billed**. It adds
render compute (`MANIM_RENDER_COST_USD`, default 0.01) to the cost.

## Rule for every paid call

Any code path that calls a paid external API must emit either a reserve-and-usage pair (when the
user should be limited) or a `usage.metered` event (cost only; see `contracts/memory-service.md`).
Web search uses free providers only today; any paid search provider added later must emit
`usage.metered`.

## Catalog check

At startup and every 10 minutes, fetch `GET /billing/v1/models`; log and count models that have no
adapter or credentials, and registry models that are not in the catalog. Never block startup.

Note (same rules as in `contracts/conversation-service.md`): `entitlements.changed` is not emitted for
Free users on catalog limit changes, so rely on the 60 s cache TTL for those; `version_hash` is an
opaque change marker, not a hash, and must not be compared with anything.

`GET /billing/v1/models` and `GET /billing/v1/me/models` carry `"think": bool` on every model: the model has a reasoning
control llm-service can turn up (Groq gpt-oss `reasoning_effort`, Claude extended thinking, GPT-6 reasoning effort, Gemini
thinking config). It is billing's claim; llm-service decides what it actually sends, and its catalog check fails when
billing says `think: true` for a model whose own `think.kind` is `none`.
