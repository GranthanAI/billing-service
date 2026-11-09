# Running billing with the payment simulator

The simulator is a persistent, Razorpay-shaped payment provider inside billing (schema
`billing_sim`). It drives the same code paths as real Razorpay: the same signatures, the same
webhook route and inbox, the same reconcile, poll and settlement jobs. Switching to Razorpay later
is configuration, not code. **It can never run in production**: billing refuses to start with
`PAYMENT_SIMULATION=true` and `ENVIRONMENT=production`.

Source of truth: `docs/Integrations/GranthanAI_Billing_Integration_Plan.md` section 5.

## Run it

> Shared dev instance: `scripts/dev_up.sh` starts API (REST 8090, gRPC **50061**) and worker (9100) with the settings below; see the README
> "Shared dev stack". gRPC is on 50061 because memory-service uses 50051 on the same machine.

1. Start the shared infrastructure and billing's Valkey: `make up` (Postgres, Kafka, Valkey), then
   `make topics`.
2. Migrate (`make migrate`; creates `billing_sim`, `billing.gateway_mode` and `payments.gateway`),
   then seed the catalog into the simulator:
   `PAYMENT_SIMULATION=true uv run python -m billing.cli pricing seed` and
   `PAYMENT_SIMULATION=true uv run python -m billing.cli catalog sync`. Plans are created in
   `billing_sim.sim_plans`, not at Razorpay.
3. Copy `.env.example` (it already sets `PAYMENT_SIMULATION=true`, `RAZORPAY_KEY_ID=rzp_sim_key`,
   `RAZORPAY_KEY_SECRET=sim_key_secret`, `RAZORPAY_WEBHOOK_SECRET=sim_webhook_secret`; in
   simulation any non-empty strings work, and the simulator signs with them exactly like Razorpay).
4. Start **both** processes with the same environment: the API (`make run-api`) and the worker
   (`uv run python -m billing.main_worker`). The worker runs the `sim.*` jobs and delivers the
   simulated webhooks; without it nothing renews and no webhook arrives.
5. `GET /healthz` shows `"payment_mode": "simulated"`; the startup log prints
   `payment_simulation_on`; `GET /billing/v1/me` shows `payment_mode: "simulated"` (the frontend
   shows a persistent banner).
6. Try it: `scripts/sim_checkout_demo.sh` takes a new user from Free to Pro by HTTP only
   (`BILLING_URL`, `GATEWAY_SECRET`; or `BASE_URL` and `USER_JWT` through the gateway) and checks
   for an `SI/...` invoice.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `PAYMENT_SIMULATION` | `false` | `true` selects the simulator gateway; `false` uses `PAYMENT_GATEWAY` (`razorpay`, or `fake` for tests) |
| `ALLOW_GATEWAY_MODE_SWITCH` | `false` | Lets billing start when the recorded gateway mode differs and `billing.payments` has rows (see the guard below) |
| `SIM_MONTH_SECONDS` | `0` | `0`: real calendar months. `600`: a monthly plan renews every 10 minutes, a yearly plan every 2 hours. Retry intervals and the settlement delay scale by `SIM_MONTH_SECONDS / 30 days` |
| `SIM_RETRY_INTERVAL` | `86400` | Seconds between failed-renewal retries (scaled by the ratio above) |
| `SIM_FAST_JOBS` | `false` | `grace.expire`, `period.close`, `subscription.poll` and `checkout.reconcile` run every 30 s; `subscription.poll` looks at periods ended 30 s ago instead of 2 h |
| `SIM_REFUND_DELAY_SECONDS` | `10` | A refund is `pending` for this long, then `processed` and `refund.processed` is sent |
| `SIM_WEBHOOK_URL` | `http://localhost:8080/billing/v1/webhooks/razorpay` | Where webhooks are POSTed. Point it at the gateway (`.../api/v1/billing/webhooks/razorpay`) to test the pass-through |
| `SIM_WEBHOOK_DELAY_SECONDS` | `1` | Delay between a state change and its webhook |
| `SIM_WEBHOOK_DUPLICATE_PCT` | `0` | Percent of webhooks sent twice (same event id; the inbox deduplicates) |
| `SIM_WEBHOOK_DROP_PCT` | `0` | Percent never delivered. `checkout.reconcile` and `subscription.poll` must recover them |
| `SIM_WEBHOOK_REORDER` | `false` | Each delivery batch is sent newest first |
| `GRACE_PERIOD_SECONDS` | unset | When set, the past-due grace period is this many seconds and `GRACE_DAYS` is ignored |
| `FRONTEND_ORIGIN` | `http://localhost:5173` | `targetOrigin` of the checkout popup's `postMessage` |

Plan scenario example (compressed time with chaos):
`SIM_MONTH_SECONDS=600 SIM_FAST_JOBS=true GRACE_PERIOD_SECONDS=300 SIM_RETRY_INTERVAL=86400
SIM_WEBHOOK_DUPLICATE_PCT=20 SIM_WEBHOOK_DROP_PCT=10 SIM_WEBHOOK_REORDER=true`.

## What the simulator does

- **Checkout.** `POST /billing/v1/checkout`, `/subscription/change`, `/subscription/resume` and
  `/wallet/topups` return `provider` and, in simulation, `simulator_url`
  (`/api/v1/billing/sim/checkout/<gateway_id>?t=<token>`; the token is an HMAC with the key
  secret of `gateway_id|user_id|expiry`, valid 30 minutes). Open it in a popup: a red "SIMULATED
  PAYMENT - no real money" banner, the amount and plan, Card / UPI / eMandate tabs (UPI hidden from
  Rs 15,000, as UPI is not offered for a single debit that large) and an OTP step. The page posts
  `{source: "granthan-sim-checkout", razorpay_payment_id, ...}` to `window.opener` for
  `FRONTEND_ORIGIN` and closes; the frontend then calls the normal `/checkout/verify`.
- **Test instruments.** Card `4111 1111 1111 1111` succeeds, `4000 0000 0000 0002` fails. UPI
  `success@razorpay` succeeds, `failure@razorpay` fails. eMandate always succeeds. OTP `000000`
  fails; any other 6 digits pass.
- **Renewals** (job `sim.renew`, every 30 s). A subscription whose `charge_at` has passed is
  charged according to its next-charge outcome: `success` (payment, new period,
  `subscription.charged`), `fail` (status `pending`, then up to 3 retries `SIM_RETRY_INTERVAL`
  apart, then `halted`) or `halt` (`pending` then `halted` at once). A halted gateway subscription
  ends the billing subscription (access falls to Free). Cancelling at cycle end cancels instead of
  charging.
- **Webhooks** (job `sim.deliver_webhooks`, every 2 s). Bodies have Razorpay's documented shape
  (`entity: "event"`, `account_id`, `event`, `contains`, `payload.<entity>.entity`, `created_at`),
  are signed with the webhook secret (`X-Razorpay-Signature`, plus `X-Razorpay-Event-Id`), and the
  exact signed bytes are POSTed. Failures retry with backoff, up to 10 attempts. A parity test
  compares every event type with `tests/fixtures/razorpay/*.json`.
- **Refunds** (`sim.refunds`) and **settlements** (`sim.settle`, T+2 days scaled): fee
  `round_half_up(amount * 236 / 10000)` (2% plus 18% GST; the fee includes the GST) and tax
  `round_half_up(fee * 18 / 118)`. Settlement lines feed `list_settlement_items`.
- **Records.** `payments.gateway = 'simulator'`. Invoices use the series `SI` (invoice), `SB` (bill
  of supply) and `SC` (credit note), for example `SI/2627/000001`, so the real GST series is never
  used. Every simulated invoice PDF carries the watermark "SIMULATED - NOT A TAX INVOICE".

## Control API (simulation only)

Needs `X-Gateway-Secret` and `X-Staff-Role: billing_admin` (no `Idempotency-Key`). Set
`H='-H "X-Gateway-Secret: $GATEWAY_SECRET" -H "X-Staff-Role: billing_admin"'`:

```bash
B=http://localhost:8080/billing/v1/sim
# make the next renewal succeed, fail or halt
curl -sX POST $B/subscriptions/$SUB/next-charge $H -H 'Content-Type: application/json' -d '{"outcome":"fail"}'
# charge now instead of waiting
curl -sX POST $B/subscriptions/$SUB/renew-now $H
# the customer cancels from their UPI app
curl -sX POST $B/subscriptions/$SUB/customer-cancel $H
# make a pending refund fail (refund.failed)
curl -sX POST $B/refunds/$REFUND/fail $H
# settle everything captured
curl -sX POST $B/settle-now $H
# everything the simulator holds for one user
curl -s "$B/state?user_id=$USER" $H
```

(The shell `$H` above is shown for readability; in a script write the headers out.)

## Gateway-mode guard

`billing.gateway_mode` records the mode (`simulator`, `razorpay` or `fake`) on the first start of
the API or the worker. At a later start with a different mode, billing refuses to start if
`billing.payments` has rows, unless `ALLOW_GATEWAY_MODE_SWITCH=true`. Use the override only for a
deliberate move to a clean staging database. Production databases must only ever have seen
`razorpay`.

## Switching to Razorpay test mode (plan phase P12)

1. Use a fresh staging database, or `ALLOW_GATEWAY_MODE_SWITCH=true` with an empty
   `billing.payments`. Set `PAYMENT_SIMULATION=false` and Razorpay **test-mode** keys
   (`RAZORPAY_KEY_ID=rzp_test_...`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`).
2. In the Razorpay dashboard add the webhook
   `https://<your-domain>/api/v1/billing/webhooks/razorpay` with the secret
   `RAZORPAY_WEBHOOK_SECRET`. Events: `subscription.*` (authenticated, activated, charged, pending,
   halted, cancelled, completed, paused, resumed, updated), `payment.*` (captured, failed),
   `refund.*` (created, processed, failed), `order.paid`, `payment_link.*` (paid, cancelled,
   expired).
3. `make catalog-sync` creates the test-mode Razorpay plans.
4. Run `runbooks/razorpay-test-mode.md` end to end. Save the real webhook bodies into
   `tests/fixtures/razorpay/recorded/` and re-run `tests/unit/test_sim_parity.py` against them; fix
   any shape difference in the simulator.
5. Production: live keys, `ENVIRONMENT=production`, `PAYMENT_SIMULATION=false`, the gateway's
   `BILLING_SIM_ROUTES=false`, real GST series. One real Rs 499 purchase by you, then an admin
   refund.

## Troubleshooting

- *Nothing renews or no webhook arrives*: the worker is not running, or `SIM_WEBHOOK_URL` does not
  reach the API. `SELECT event, status, attempts, last_error FROM billing_sim.sim_webhook_outbox
  ORDER BY id DESC LIMIT 20;`
- *The user paid but is still `incomplete`*: the webhook was dropped or late; `checkout.reconcile`
  (every 5 min, 30 s with `SIM_FAST_JOBS`) applies the payment after 15 minutes.
- *`GATEWAY_MODE` refusal at start*: see the guard above.
- *Sim routes answer 404*: the process was not started with `PAYMENT_SIMULATION=true`.
