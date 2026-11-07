# What file-service and billing-service owe each other

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
