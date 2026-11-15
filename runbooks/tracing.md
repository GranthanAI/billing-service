# Following one request through the stack (Windows dev, Git Bash)

One chat message touches: **gateway -> conversation-service -> billing gRPC -> (outbox) Kafka -> llm-service -> Kafka ->
billing worker -> memory-service**. This is how to follow it with one id, and where the id stops.

## What carries what

| Hop | What identifies the request | Where it is logged |
| --- | --- | --- |
| Client -> gateway | `X-Correlation-ID` header (the gateway makes one if the client sent none) and echoes it in the response | `.logs/gateway.log` as `correlation_id=...` (only on lines the gateway logs: errors and warnings) |
| Gateway -> conversation | the same `X-Correlation-ID`, plus `X-User-Id` | `.logs/conversation.log` as `correlation_id=...` on every line of that request |
| Conversation -> billing gRPC | gRPC metadata `x-correlation-id` (the same id) | `billing-service/.dev/logs/api.log` as `correlation_id` on every line billing logs for that call (billing logs a line only for warnings and errors, so a normal allow leaves none) |
| Conversation -> Kafka | the outbox payload `trace_context.traceparent` is the correlation id; the event also has `conversation_id`, `message_id` (the user message) and `response_message_id` | Kafka topic `chat.message.created` |
| llm-service | the event's `trace_context`, `conversation_id`, `message_id`, the billing `hold_id` | `.logs/llm.log` as `trace_id` / `request_id`, `conversation_id` |
| llm-service -> Kafka -> billing worker | `chat.response.generated` carries `hold_id`, `message_id`, `model`, tokens | `billing-service/.dev/logs/worker.log` (usage applied / dead-lettered) |
| Memory | NO correlation id. Join by `conversation_id` | `.logs/memory.log` |

So the **correlation id** follows you from the gateway to billing gRPC. From Kafka onwards use the **ids inside the events**
(`conversation_id`, `response_message_id`, `hold_id`). The `hold_id` is the thread that ties billing's reserve to the usage
event that settles it.

## Step by step

All commands run from `C:\Users\hp\Desktop\Granthan` in Git Bash. Logs: `.logs/*.log` (root Makefile services) and
`billing-service/.dev/logs/*.log` (billing).

1. **Send a request with an id you choose**

   ```bash
   CID=trace-$(date +%s)
   curl -i -H "Authorization: Bearer $TOKEN" -H "X-Correlation-ID: $CID" -H "Content-Type: application/json" \
     -d '{"content":"hi","sender":"user","model":"openai/gpt-oss-20b"}' \
     http://localhost:8080/api/v1/conversations/$CONV/messages
   ```

   The response carries `response_message_id` and the `X-Correlation-ID` header. Keep both.

2. **Gateway and conversation**

   ```bash
   grep -a "$CID" .logs/gateway.log .logs/conversation.log
   ```

   PowerShell: `Select-String -Path .logs\gateway.log,.logs\conversation.log -Pattern $CID`.
   The conversation lines show the conversation id; copy it as `$CONV` and the response id as `$RID`.

3. **Billing gRPC (the reserve)**

   ```bash
   grep -a "$CID" billing-service/.dev/logs/api.log
   ```

   Billing logs a warning or error line here only when something is wrong (a deny is not logged). To see EVERY reserve, use the
   metric instead: `curl -s localhost:8090/metrics | grep billing_reserve_requests_total`. For the hold of an allowed reserve,
   read it from the send response's `billing` block (the message's `granted_group`, `granted_model`, `remaining_today`) or from
   the llm event (step 5).

4. **Kafka: the event conversation published**

   ```bash
   docker exec graphgpt-kafka /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server localhost:9092 --topic chat.message.created \
     --from-beginning --max-messages 200 --timeout-ms 8000 2>/dev/null | grep "$RID"
   ```

   (The Kafka container name is the one `scripts/dev_status.sh` reports under "kafka"; `docker ps` shows it.) The payload has
   `trace_context.traceparent` = your correlation id, the `billing` block (`hold_id`, `granted_group`, `granted_model`,
   `allowed_models`) and the ids to follow next.

5. **llm-service**

   ```bash
   grep -a "$CONV" .logs/llm.log | tail -20
   ```

   Look for the group chain it built (the models it tried, in order), `fallback_used`, and any provider error. A model that
   cannot answer is reported by `GET /internal/models/availability` (header `X-Service-Token`).

6. **Usage reaches billing (the worker)**

   ```bash
   grep -a "$RID" billing-service/.dev/logs/worker.log
   docker exec graphgpt-kafka /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server localhost:9092 --topic chat.response.generated \
     --from-beginning --max-messages 200 --timeout-ms 8000 2>/dev/null | grep "$RID"
   ```

   The event has the `hold_id`, `model`, `model_group` and token counts. A usage event that could not be applied is
   dead-lettered (`billing_dlq_messages_total`, alert `BillingUsageNotBilled`).

7. **The ledger and the counters**

   ```bash
   GS=$(grep ^GATEWAY_SHARED_SECRET billing-service/.env.dev | cut -d= -f2-)
   curl -s -H "X-Gateway-Secret: $GS" -H "X-User-Id: <user id>" localhost:8090/billing/v1/me | python -m json.tool | head -40
   ```

   `usage_today` shows the counters; `scripts/billing-admin.http` has the staff reads (account view, costs).

8. **Memory-service**

   ```bash
   grep -a "$CONV" .logs/memory.log | tail
   ```

   Memory has no correlation id: only the conversation id joins it to the rest.

## When the trail stops

- **No `$CID` line in conversation.log**: the request never got past the gateway (401, 429 or 5xx). Check `gateway.log`.
- **Conversation shows the id but billing shows nothing**: normal for an allow. If the reserve failed, conversation logs
  `billing_degraded_*` or the breaker lines; `billing_breaker_open` is 1 on `localhost:8002/metrics`.
- **No `chat.message.created` for `$RID`**: the outbox did not publish. `curl -s localhost:8090/metrics | grep outbox` is billing's
  own outbox; conversation's outbox worker logs to `.logs/conversation.log`.
- **llm-service answered with a different model than you named**: look at `granted_model` and `allowed_models` in the event; the
  chain never leaves `allowed_models`. An unavailable model is refused at the reserve (409 `model.unavailable`), never swapped.
- **Usage never reaches billing**: `/opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server localhost:9092 --describe --group billing-usage` shows the lag; the worker log shows
  decode errors.
