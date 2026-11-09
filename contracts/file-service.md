# What file-service and billing-service owe each other

> **Local dev address:** the shared dev instance (`scripts/dev_up.sh`) serves REST on `http://127.0.0.1:8090` and gRPC on `127.0.0.1:50061` (not 50051: memory-service owns that port). Always `127.0.0.1`, never `localhost` (Windows resolves it to IPv6 first). The service tokens and the gateway secret are printed by `dev_up.sh` and kept in `.env.dev`.


## billing calls file-service (bearer `FILE_SERVICE_TOKEN`, 5 s timeout, 3 retries)

`GET {FILE_SERVICE_URL}/internal/storage/usage?user_id={user_id}`

Header: `Authorization: Bearer <service token>` (the same internal token file-service gives its other
service callers; not the gateway secret).

200 response, always (a user who stores nothing gets `0`):

```json
{"user_id": "0190c0de-0000-7000-8000-000000000001", "bytes_used": 0, "measured_at": "2026-11-01T04:30:00Z"}
```

- `bytes_used`: integer, the bytes the user stores right now, summed over their files.
- `measured_at`: ISO 8601 with offset; when file-service took the measurement.
- Retries on timeouts, connection errors, 429 and 5xx (3 retries, 0.2 s then doubling). Any other
  4xx fails at once. One user failing never stops the monthly batch; billing logs it and bills that
  user on the next run of `storage.bill` (it is idempotent per user and month).

Billing calls this only for users whose winning grant is Pay as you Go, once a month (see below).
Subscription plans have storage limits that file-service enforces itself.

## billing publishes (Kafka, JSON)

### `storage.payment_required`

Sent when a Pay as you Go user's wallet could not cover the monthly storage charge. The wallet is
debited with whatever it held (it never goes negative) and the rest is the shortfall.

```json
{
  "event_id": "...", "occurred_at": "2026-11-01T04:30:00Z", "schema_version": "1.0",
  "user_id": "...", "billing_account_id": "...",
  "over_gb": 16, "shortfall_paise": 2360, "month": "202610"
}
```

- `over_gb`: whole GB above the free 10 GiB for that month (rounded up).
- `shortfall_paise`: what is still owed, GST included (the amount the user is shown).
- `month`: the IST month billed, `YYYYMM`.

**file-service should block new uploads** for `user_id` when it receives this, and show that the
user needs to top up. It is not sent again for the same user and month. Billing does not send an
"all clear": file-service re-checks by asking the user to retry after a top-up, or by calling
`GET /billing/v1/wallet` through the gateway; uploads can be allowed again once the balance is
positive.

## file-service publishes, billing consumes

### `file.youtube.ingested` (existing)

One YouTube video was ingested; billing charges it (a plan's monthly allowance, or credits).
`{event_id, occurred_at, user_id, source_id, hold_id | null, status: success|error}`.
`source_id` is the video id and the dedupe key with `event_id`. file-service must have called
`CheckAndReserve(kind = YOUTUBE_VIDEO)` first and copy the returned `hold_id`.

## Pricing, for reference

Storage over 10 GiB is billed to Pay as you Go users at `storage_over_10gb_per_gb_month`
(1,000 paise, GST included, per started GB per month) on the 1st of the month at 03:30 IST for the
month that just ended. 1 GiB = 1,073,741,824 bytes.

## Integration plan, phase P1 additions

Source: `docs/Integrations/GranthanAI_Billing_Integration_Plan.md` sections 4.2, 4.4.4, 4.4.7, 4.6
to 4.8. The plan wins where it disagrees with this file.

### Limits file-service enforces (plan 4.8)

| Limit (from `GetEntitlements`) | Free / Pro / Max | Where |
| --- | --- | --- |
| `file_size_mb` | 10 / 50 / 100 | `POST /files/upload/initiate`: declared size at most the limit |
| `storage_mb` | 100 MB / 2 GB / 10 GB | same call: stored bytes plus the new size at most the limit |

A breach is `403` with `detail.code: "plan_limit"` and `message_key` `limit.file_size_mb` or
`limit.storage_mb`. The global `MAX_UPLOAD_SIZE_BYTES` stays as a hard ceiling. Track `bytes_stored`
per user (active documents; update on complete and on delete). `-1` means unlimited.

### YouTube sources: reserve before, event after

- Before creating a source and before a retry: `CheckAndReserve(kind = YOUTUBE_VIDEO,
  idempotency_key = "{document_id}:{attempt}")`. `attempt` starts at 1 and goes up by 1 on each
  retry route call. A deny is the billing deny shape with `429` / `limit.monthly`.
- Billing unreachable: deny new YouTube sources with `503` (they cost proxy money).
- On the final success or failure, publish `file.youtube.ingested` through your outbox:

```json
{"event_type": "file.youtube.ingested", "schema_version": "1.0", "event_id": "...",
 "occurred_at": "2026-11-08T10:00:00Z", "user_id": "...", "source_id": "<document_id>:<attempt>",
 "document_id": "...", "attempt": 1, "hold_id": "... or null", "status": "success|error",
 "proxy_bytes": 0}
```

Billing deduplicates usage on `(kind, source_id)`. It builds the usage id as
`"{document_id}:{attempt}"` (attempt defaults to 1 when absent), so **a retry that succeeds after a
failed first try is billed**. Send one event per attempt. `proxy_bytes` is optional.

### Entitlements

`GetEntitlements(user_id)` cached 60 s per user; consume **`entitlements.changed`** (schema in
`contracts/conversation-service.md`) and `user.deleted` and drop that user's entry. Billing
unreachable: cached value up to 1 hour, then Free limits.

### Later (Pay as you Go, v3)

- `GET /internal/storage/usage?user_id=` (service token) returns
  `{"user_id", "bytes_used", "measured_at"}`; billing's `storage.bill` job calls it.
- Consume `storage.payment_required`: block new uploads for that user until `entitlements.changed`
  or a top-up arrives; the message is `credits.empty`.
- `user.deleted`: schedule deletion of the user's files per the retention policy.

Note (same rules as in `contracts/conversation-service.md`): `entitlements.changed` is not emitted for
Free users on catalog limit changes, so rely on the 60 s cache TTL for those; `version_hash` is an
opaque change marker, not a hash, and must not be compared with anything.
