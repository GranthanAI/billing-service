# Stage H: routes and settings for organisation-service (P1 B1-B4)

**Tests (reduced scope, by your instruction: no big sweeps):** new unit file `tests/unit/test_org_internal_routes.py` 8 passed;
`tests/unit/test_settings_environment.py` (with the new org-service config tests) 28 passed together with it.
`ruff` on the changed files is at its baseline (14 pre-existing E501), `mypy` shows only the 9 pre-existing `catalog.py`
errors, `lint-imports` 4 kept / 0 broken.
**Written but NOT run:** the integration tests in `tests/integration/test_orgs.py` (subscription shape for org_pro/org_max,
pending decrease, `NO_SUBSCRIPTION` cases, token check, usage with leavers / without personal usage / one query / empty
without an account). They need the container services and I did not start new containers. Run
`pytest tests/integration/test_orgs.py` before relying on B1/B2 against a real database. Not run: `make test-all`.

**What changed**
- B1: `GET /internal/orgs/{org_id}/subscription` -> `{subscription_id, plan_code, variant, seats, pending_seats, status, period_end}`.
  404 problem+json `code: NO_SUBSCRIPTION` when there is no billing account or no live subscription; an org-only plan
  override is not a subscription. New `NoSubscription` error and `NO_SUBSCRIPTION: 404` in the status map.
- B2: usage report reads by billing account in ONE query (`AdminRepo.account_usage`). Leavers are included, personal usage is
  not, no account gives empty rows; the route no longer asks org-service for members. Response shape unchanged.
- B3: `scripts/topics.txt` gains `org.policy.changed`, `org.invite.created`, `org.pilot.ending`, `organisation.dlq`;
  `contracts/org-service.md` rewritten to what org-service serves and emits.
- B4: `.env.example` documents `ORG_SERVICE_URL` and `ORG_SERVICE_TOKEN`; in production the service refuses to start
  without either. No real `.env` / `.env.dev` was edited.

**Decisions for you**
- One token both ways: `ORG_SERVICE_TOKEN` = org-service `BILLING_SERVICE_TOKEN` = its `billing:` entry in
  `INTERNAL_SERVICE_TOKENS`. Set all three alike; set the URL and token in billing's real env by hand.
- Real dev billing (8090) was not running, so nothing here was checked against it.
- Nothing pushed.
