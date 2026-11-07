# What api-gateway must do with billing-service's REST API

billing-service trusts the identity headers on its REST routes (`/billing/v1/...` and
`/admin/billing/v1/...`) because api-gateway sets them after verifying the JWT. That is only safe if
**nothing but api-gateway can reach those routes with forged headers**, so billing checks a shared
secret on every call.

## The shared secret

- api-gateway sends `X-Gateway-Secret: <secret>` on **every** request it forwards to billing.
- billing accepts any value in `GATEWAY_SHARED_SECRET` (comma-separated, so it can rotate: add the new
  secret to billing, switch api-gateway, then remove the old one). The comparison is constant time.
- A missing or wrong secret gets `401` (problem+json, `code: UNAUTHENTICATED`) and **no handler
  runs**: nothing is written. If billing has no secret configured it refuses everything; the
  process also refuses to start without one.
- Not required (public): `GET /billing/v1/plans`, `GET /billing/v1/models`,
  `POST /billing/v1/webhooks/razorpay` (Razorpay signs it; do not route it through the JWT check),
  `GET /healthz`, `GET /readyz`, `GET /metrics`.
- Treat the secret like any other credential: it is a secret in the deployment (OpenBao / SOPS), and
  billing is only reachable on the cluster network.

## The identity headers

- api-gateway **strips any `X-User-Id`, `X-Staff-Role` and `X-Correlation-ID` the client sent** before
  it sets its own. A client must never be able to choose them.
- `X-User-Id` is the `sub` of the verified JWT (a UUID). For admin routes it is the staff member's id.
- **`X-Staff-Role` is set only from the staff claims in the JWT**: `billing_admin` (may write) or
  `support` (read only). A normal user's request must arrive with no `X-Staff-Role` at all. billing
  rejects an unknown role with `401` and a `support` write with `403`.
- `X-Correlation-ID`: pass the request's id through; billing makes one when it is missing.
- Send `Idempotency-Key` (a UUID per user action) on every `POST` to billing except the Razorpay
  webhook. Admin `POST`s need one too.

## What billing never does

It never takes a user id from a request body or query string, and it never calls api-gateway back.

## Pay as you Go routes (only when `PAYG_ENABLED=true`; see `runbooks/payg-launch.md`)

Signed-in user (JWT, `X-User-Id`), same rules as the other `/billing/*` routes:
`GET /billing/v1/wallet`, `GET /billing/v1/wallet/transactions`, `POST /billing/v1/wallet/topups`,
`POST /billing/v1/wallet/topups/verify`, `PUT /billing/v1/wallet/settings`. Amounts come as
`*_paise` (GST included, what the user paid or was quoted) and `*_taxable_paise` (what the wallet
holds). Staff (`billing_admin` only, a reason in every body):
`POST /admin/billing/v1/orgs/{org_id}/wallet/topups`, `.../topups/{id}/mark-paid`,
`.../topups/{id}/cancel`. With the flag off the write routes answer 404, and the two read routes
answer 404 only for a user who never had a wallet.
