# What org-service and billing-service owe each other

> **Local dev address:** the shared dev instance (`scripts/dev_up.sh`) serves REST on `http://127.0.0.1:8090` and gRPC on `127.0.0.1:50061` (not 50051: memory-service owns that port). Always `127.0.0.1`, never `localhost` (Windows resolves it to IPv6 first). The service tokens and the gateway secret are printed by `dev_up.sh` and kept in `.env.dev`.


## billing calls org-service (bearer `ORG_SERVICE_TOKEN`, 2 s timeout, one retry)

- `GET /internal/users/{user_id}/membership` -> `{org_id, org_type: college|company, role, status}`,
  404 when the user belongs to no org. Only `status = active` gives the org plan.
- `GET /internal/orgs/{org_id}` -> `{org_id, name, type, gstin, billing_email, state_code,
  member_spend_cap_paise}`. `state_code` (GST, two digits) is required to invoice.
- `GET /internal/orgs/{org_id}/members` -> `{user_ids: [...]}`. The member count is what billing
  checks for an organisation **pilot** (at most 25 members and 30 days), every time.
  - No org-service configured: pilots are refused ("org-service is required for organisation pilots").
  - Configured but unreachable: the staff request fails with 503 `GATEWAY_UNAVAILABLE`.

`member_spend_cap_paise` (Pay as you Go):
- The most one member may spend from the organisation's **wallet** per IST month.
- In display paise (GST included); billing converts to taxable (`gross * 100 / 118`, half up) to compare. `null` = no cap.
- Applies only when the organisation's plan wins and it has a wallet; never to a personal wallet.
- Changes reach reserves within the directory cache (60 s) plus the entitlement cache (10 min).
- A member over the cap gets `credits.member_cap` (HTTP 402, see `conversation-service.md`).

Answers are cached in Valkey for `ORG_DIRECTORY_TTL_SECONDS` (60). If org-service is down, billing
falls back to the user's personal plan for 30 s at a time and never fails the request.

## org-service publishes (Kafka, JSON)

`org.member.joined` and `org.member.left`: `{event_id, user_id, org_id, occurred_at}`. billing clears
the member's cached membership and entitlement; on a join it emits
`entitlement.personal_plan_redundant` when the user's own paid plan is no better than the org's.
Failures go to `billing.org.dlq`.

## org-service calls billing

- `GET /internal/orgs/{org_id}/usage?from=YYYY-MM-DD&to=YYYY-MM-DD` with the same bearer token (not
  the gateway secret): per member and model group, messages and billable units.

Subscribing an org, changing seats and recording a bank transfer are staff actions in
`/admin/billing/v1` (`orgs/{id}/subscriptions`, `orgs/{id}/seats`, `invoices/{id}/mark-paid`,
`invoices/{id}/payment-link`).
