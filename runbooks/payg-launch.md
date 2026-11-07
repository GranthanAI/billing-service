# Turning Pay as you Go on

`PAYG_ENABLED=false` (v1) closes it completely. Items marked **(you)** need a person or another team.

## Before the switch
- **(you)** CA sign-off on GST at top-up (18% charged when paid; wallet holds the taxable value; 12-month expiry booked as breakage).
- **(you)** Business decisions: refund policy for unused credits, storage-shortfall behaviour in file-service, org wallet before personal wallet for org members.
- Kafka topics exist: `storage.payment_required`, `wallet.credits_expiring` (`make topics`). file-service consumes the first; notifications the second.
- **(you)** api-gateway routes: `GET /billing/v1/wallet`, `GET .../wallet/transactions`, `POST .../wallet/topups`, `POST .../wallet/topups/verify`, `PUT .../wallet/settings`; staff `/admin/billing/v1/orgs/{id}/wallet/*`.
- **(you)** Frontend wallet pages: balance (`balance_paise` = GST included), packs, history, cap, `credits.empty` / `credits.spend_cap` / `credits.member_cap`, Ultra (`upgrade_to: "payg"`, `funded_by: "credits"`).
- **(you)** Razorpay webhooks on: `payment.captured` (wallet orders) and `payment_link.paid` (org top-ups, invoices).
- `FILE_SERVICE_URL`, `FILE_SERVICE_TOKEN` set on the worker (for `storage.bill`).

## Staging run (you)
- Top up, use until exhausted, top up again; the wallet shows the pack price; Valkey equals the ledger.
- Org: create a top-up, pay the link, check the credit and the B2B invoice; repeat with `mark-paid`; cap one member.
- Storage: `storage.bill` for a user over 10 GiB; empty wallet sends `storage.payment_required`.
- Reconciliation green for the staging day.

## The switch
- `PAYG_ENABLED=true` on the API **and** the worker; restart both.
- Watch `billing_storage_billed_total{result="error"}`, `wallet_resync` logs, reconciliation, and that `payg_topup_while_disabled` never appears.

## Turning it off
- Set `PAYG_ENABLED=false` on both and restart. Spending stops; balances are **kept**.
- Write routes are 404; users with a wallet can still read it; wallet jobs skip and log `wallet_job_skipped`.
- A top-up paid while off is recorded but not credited (`payg_topup_while_disabled`): refund it.
- Lots past 12 months expire only after the flag is back on.
- **Question for the business owner:** do holders of unused credits get a refund if it stays off, and on what terms?
