# Billing emails

Billing sends seven transactional emails. **Only the worker sends mail**: the API process never
builds an email sender, it only queues jobs. Source: integration plan, phase P2.

| Template | Sent when | Attachment |
| --- | --- | --- |
| `receipt` | the invoice PDF is rendered (tax invoice, or "Bill of supply" when GST is off) | the invoice PDF, named like `SI-2627-000001.pdf` (`/` becomes `-`) |
| `payment_failed` | a renewal charge fails (`ChargeFailed`) | none; has the grace end date (IST) and a Pay now link |
| `subscription_suspended` | the grace period ends (`GraceExpired`) | none |
| `renewal_upcoming` | `renewal.remind`, 24 to 48 hours before a renewal | none (courtesy only; the bank sends its own pre-debit notice) |
| `subscription_cancelled` | the user cancels, or the mandate is cancelled in the bank or UPI app | none |
| `refund_issued` | the provider reports the refund `processed` | the credit note PDF |
| `credits_expiring` | `credits.expire` finds credits expiring within 30 days (Pay as you Go only) | none |

Each one is a job `email.send` with the dedupe key `<template>:<entity>`, queued **in the same
transaction** as the change that triggers it, so a rollback queues nothing and a replayed webhook
or job queues nothing more. Entity: invoice id (receipt), subscription id + period end
(payment_failed, suspended, cancelled, renewal_upcoming), refund id, account id + expiry date
(credits_expiring). The job retries 5 times (2, 4, 8, 16 seconds apart), then is `dead`. Every
failed attempt counts in `billing_email_failed_total{template}`.

Emails never contain a presigned URL or a token. Links go to the frontend
(`FRONTEND_ORIGIN/settings/billing`) and, for Pay now, the same `payment_action_url` that
`GET /billing/v1/me` returns. The recipient is `billing_accounts.billing_email`; with none, no email
is queued (`email_skipped_no_recipient` in the log, `billing_email_skipped_total{reason}`).
Addresses are never logged in full.

## Settings (all on the worker)

| Variable | Default | Meaning |
| --- | --- | --- |
| `EMAIL_MODE` | `console` | `console` (logs subject, redacted recipient, body, attachment names and sizes), `file` (one `.eml` per message) or `smtp`. `console` and `file` are **refused when `ENVIRONMENT=production`** |
| `EMAIL_ENABLED` | `true` | `false` queues no emails at all |
| `EMAIL_OUTBOX_DIR` | `./.email-outbox` | where `file` mode writes (git-ignored) |
| `EMAIL_FROM` | | the sender; **required for `smtp`** |
| `EMAIL_REPLY_TO` | | optional `Reply-To` |
| `SUPPORT_EMAIL` | `support@granthan.ai` | the support contact printed in every email |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD` | `587` | the same names as auth-service; **`SMTP_HOST` is required for `smtp`** |
| `SMTP_SECURITY` | `auto` | `auto`: STARTTLS on 587, SSL on 465, none on other ports; or `starttls`, `ssl`, `none` (a local test server) |
| `FRONTEND_ORIGIN` | `http://localhost:5173` | the host of every link in an email |
| `PAYMENT_SIMULATION` | `false` | `true`: subjects start with `[SIMULATED] ` and every email says "This is a simulated payment. No money moved." |

The worker refuses to start with an unsafe mode, and so does the API (same check).
The message id is derived from the idempotency key, so a retried send keeps its `Message-ID`; in
`file` mode a retry rewrites the same file.

## File mode on Windows

1. In the worker's environment: `EMAIL_MODE=file`, `EMAIL_FROM=GranthanAI <billing@granthan.ai>` and
   optionally `EMAIL_OUTBOX_DIR=C:\Users\you\Desktop\mail-out` (the default is `.email-outbox` in the
   folder the worker runs from).
2. Start the worker. Make a payment (`scripts/sim_checkout_demo.sh`) and open the `.eml` files in the
   folder by double-clicking: Outlook or Windows Mail shows the HTML, the text part and the PDF.
3. `scripts/sim_email_demo.sh` does the whole tour: a receipt, a forced failed renewal and the
   suspension. Start the worker with `GRACE_PERIOD_SECONDS=60 SIM_FAST_JOBS=true` first.

## A real SMTP provider

Set `EMAIL_MODE=smtp`, `SMTP_HOST`, `SMTP_PORT` (587 for STARTTLS, 465 for SSL), `SMTP_USERNAME`,
`SMTP_PASSWORD` (a secret, never in git), `EMAIL_FROM` (an address the provider has verified) and
optionally `EMAIL_REPLY_TO`. Use the same provider settings as auth-service. Check SPF and DKIM for
the sending domain with your provider before launch. Send one test: make a simulated or test-mode
payment and look for the receipt.

## Failed and dead email jobs

```sql
-- queued, running, retrying or dead emails, newest first
SELECT id, status, attempts, max_attempts, run_at, dedupe_key, left(last_error, 120) AS error
  FROM billing.jobs WHERE kind = 'email.send' ORDER BY id DESC LIMIT 30;
```

A job that failed less than 5 times is `queued` again with a later `run_at`; the usual causes are
the SMTP provider being down (fix it, the retries pick it up) or `the PDF of ... is not available
yet` (the object store has not got the invoice yet; it resolves by itself).

To replay a **dead** job after the cause is fixed (it is one SQL update; the dedupe key guarantees
one email per entity, so a replay cannot duplicate a mail that was already sent):

```sql
UPDATE billing.jobs SET status = 'queued', attempts = 0, run_at = now(), locked_until = NULL
 WHERE kind = 'email.send' AND status = 'dead' AND id = :id;   -- or drop the id filter to replay all
```

If a mail was sent but the job is dead (the provider accepted it and the connection broke), the
replay may send it a second time with the same `Message-ID`; mail clients usually collapse it.

An email that must never be sent (for example a stale receipt) can be removed:
`UPDATE billing.jobs SET status = 'done' WHERE id = :id AND status IN ('queued', 'dead');`

Metrics: `billing_email_sent_total{template}`, `billing_email_failed_total{template}` (each failed
attempt), `billing_email_skipped_total{template,reason}` and `billing_jobs_dead_total{kind="email.send"}`.
