# What memory-service must do with billing-service

Source: `docs/Integrations/GranthanAI_Billing_Integration_Plan.md` sections 4.4.5, 4.4.7 and 4.9
(phase P8). The plan wins where it disagrees with this file. memory-service makes paid LLM calls
(Groq `openai/gpt-oss-20b`) for summaries and fact extraction, so billing must hear about the cost.

## Cost metering: publish `usage.metered` 1.0

Wrap the Groq client so every completion records the provider-reported usage
(`usage.prompt_tokens`, `usage.completion_tokens`). Publish one event per job (a summary or a fact
extraction), through an outbox or a producer with retries, to topic `usage.metered` keyed by
`user_id`:

```json
{"event_type": "usage.metered", "schema_version": "1.0", "event_id": "...",
 "occurred_at": "2026-11-08T10:00:00Z", "producer": "memory-service", "user_id": "...",
 "conversation_id": "... or null", "request_message_id": "... or null",
 "feature": "memory.summary",
 "calls": [{"provider": "groq", "model": "openai/gpt-oss-20b", "purpose": "memory.summary",
            "input_tokens": 2000, "cached_input_tokens": 0, "output_tokens": 300,
            "usage_estimated": false}],
 "fixed_cost_micro_inr": 0}
```

- `feature` is `memory.summary`, `memory.facts` or a later feature name; `purpose` repeats it.
- `model` must be a catalog ID that has a price in billing. An unknown model goes to
  `billing.usage.dlq` and can be replayed once it is priced.
- Money is integer micro-rupees; token counts are the provider's own integers (>= 0).
- `event_id` is the idempotency key: send the same id when you retry a publish.

What billing does (consumer group `billing-usage`): stores a `usage_events` row with `kind = "aux"`,
`source_id = event_id`, price 0 and units 0, rated by the price book at `occurred_at` plus
`fixed_cost_micro_inr`. It **never touches counters or holds**: memory work is cost only, and it
never counts against the user's messages. Example: 2,000 input and 300 output tokens on
`openai/gpt-oss-20b` at US$0.075 and US$0.30 per million, at 95 INR per US$, is 22,800 micro-INR.

## `memory_facts` limit

Call `GetEntitlements(user_id)` (cache 60 s per user) and read `limits.memory_facts` (Free 50, Pro
500, Max 2,000; `-1` is unlimited). At the cap do not store new facts: log
`memory_fact_cap_reached` and keep the existing ones (read-only beyond the cap). Replacing the
lowest-importance fact instead is a decision for memory-service's owner.

## Entitlements and deletion

Consume **`entitlements.changed`** (schema in `contracts/conversation-service.md`) and drop that
user's cache entry. Billing unreachable: cached value up to 1 hour, then Free limits. Consume
`user.deleted` and delete the user's memories.

## Rule

Any new code path that calls a paid external API must emit a reserve-and-usage pair or a
`usage.metered` event. A change that adds a paid call without one is rejected.
