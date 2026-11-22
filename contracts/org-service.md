# What organisation-service and billing-service owe each other

> **Local dev address:** the shared dev instance (`scripts/dev_up.sh`) serves REST on `http://127.0.0.1:8090` and gRPC on `127.0.0.1:50061` (not 50051: memory-service owns that port). organisation-service serves `http://127.0.0.1:8010`. Always `127.0.0.1`, never `localhost` (Windows resolves it to IPv6 first). The service tokens and the gateway secret are printed by `dev_up.sh` and kept in `.env.dev`.

This file matches what organisation-service (`../organisation-service`, built and tested) actually serves and emits. Its own copy of the contract is `../organisation-service/docs/PHASES.md` (Contracts) and `docs/lld.md`.

## One token, both ways

`ORG_SERVICE_TOKEN` (billing) is a bearer token used in BOTH directions:
- billing sends it to organisation-service, which accepts it as the `billing:` entry of its `INTERNAL_SERVICE_TOKENS`;
- organisation-service sends it to billing on `/internal/orgs/{id}/usage` and `/subscription`, from its `BILLING_SERVICE_TOKEN`.

So `ORG_SERVICE_TOKEN` = organisation-service `BILLING_SERVICE_TOKEN` = the `billing:` token in its `INTERNAL_SERVICE_TOKENS` (dev value in all three places: `dev-billing-token`). In production all three must be set (32+ characters); billing refuses to start without `ORG_SERVICE_URL` and `ORG_SERVICE_TOKEN`.

## billing calls organisation-service (bearer `ORG_SERVICE_TOKEN`, 2 s timeout, one retry)

- `GET /internal/memberships/{user_id}` (the alias `GET /internal/users/{user_id}/membership` answers the same) -> `{org_id, org_type: college|company, role: owner|admin|member, status}`, 404 when the user is in no organisation.
  - `status` is `active` while the organisation can take members (pilot or active). When the organisation is `suspended`, `pilot_expired` or `closed`, the membership is still there and `status` is that organisation status. **Only `status = active` grants the org plan.**
- `GET /internal/orgs/{org_id}` -> `{org_id, name, type, gstin, billing_email, state_code, member_spend_cap_paise}` (404 unknown). `state_code` (GST, two digits) is required to invoice. Closed organisations are still answered (invoices may need them).
- `GET /internal/orgs/{org_id}/members` -> `{user_ids: [...]}`, active members only, oldest first, at most 10,000 (422 above). The member count is what billing checks for an organisation **pilot** (at most 25 members and 30 days), every time.
  - No org-service configured: pilots are refused ("org-service is required for organisation pilots").
  - Configured but unreachable: the staff request fails with 503 `GATEWAY_UNAVAILABLE`.

`member_spend_cap_paise` (Pay as you Go):
- The most one member may spend from the organisation's **wallet** per IST month.
- In display paise (GST included); billing converts to taxable (`gross * 100 / 118`, half up) to compare. `null` = no cap.
- Set by the organisation's owner or admin (`PUT /org/v1/orgs/{id}/policies`, payg only); changes reach reserves within the directory cache (60 s) plus the entitlement cache (10 min).
- A member over the cap gets `credits.member_cap` (HTTP 402, see `conversation-service.md`).

Answers are cached in Valkey for `ORG_DIRECTORY_TTL_SECONDS` (60). If org-service is down, billing falls back to the user's personal plan for 30 s at a time and never fails the request.

## organisation-service publishes (Kafka, JSON, keyed by `user_id`)

`org.member.joined`: `{event_id, schema_version, occurred_at, org_id, user_id, role, org_type, source}` where `source` is `staff | invite | csv | link | domain`.
`org.member.left`: `{event_id, schema_version, occurred_at, org_id, user_id, role, org_type, reason}` where `reason` is `left | removed | user_deleted | org_closed`.

billing clears the member's cached membership and entitlement; on a join it emits `entitlement.personal_plan_redundant` when the user's own paid plan is no better than the org's. Failures go to `billing.org.dlq`. The other topics organisation-service produces (`org.policy.changed`, `org.invite.created`, `org.pilot.ending`) and its dead-letter topic `organisation.dlq` are not consumed by billing; `scripts/topics.txt` lists them so `topics.sh` creates them.

## organisation-service calls billing (bearer `ORG_SERVICE_TOKEN`)

- `GET /internal/orgs/{org_id}/usage?from=YYYY-MM-DD&to=YYYY-MM-DD[&format=csv]` -> `{org_id, from, to, rows: [{user_id, day, model_group, messages, billable_units}]}`. IST days. The rows are what the **organisation's billing account** was billed (one query), so a member who left mid-period is included and a member's personal-plan usage is not. An organisation with no billing account answers an empty `rows`.
- `GET /internal/orgs/{org_id}/subscription` -> `{subscription_id, plan_code: org_pro|org_max, variant: college|company, seats, pending_seats (a decrease that applies at renewal, else null), status, period_end (ISO UTC)}`. `status` is the subscription status (`active`, `past_due`, `suspended`, `cancelling`, ...). **404 problem+json with `code: NO_SUBSCRIPTION`** when the organisation has no billing account or no live subscription (an organisation-only plan override, used for pilots, is not a subscription). organisation-service tells this 404 from "the route does not exist" by that code.

Subscribing an org, changing seats and recording a bank transfer are staff actions in `/admin/billing/v1` (`orgs/{id}/subscriptions`, `orgs/{id}/seats`, `invoices/{id}/mark-paid`, `invoices/{id}/payment-link`). organisation-service learns about them from `subscription.changed` (only events with `owner_type = org` matter, and only as a hint: it then calls `/subscription`) and a nightly refresh.
