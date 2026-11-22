# Stage I: think longer, the limit and the model flag (quick wins, prompt 1: B1-B3)

**Tests:** `tests/unit` + `tests/property` (not slow) 514 passed, 2 failed. Both failures are in
`tests/unit/domain/test_catalog_entitlements.py` (`test_fixture_limits`, `test_price_for_and_models`) and are **pre-existing**
(`test_fixture_limits` fails on the stashed baseline too; the other asserts Free cannot use claude-haiku-5-5 although plans.yaml lists it as an extra).
`ruff` on the changed src files is at baseline (only the new migration docstring is over 100 columns, like 0022); `mypy` shows only the 9 pre-existing `catalog.py` errors; `lint-imports` 4 kept / 0 broken. `make lint` as a whole was already red (267 ruff errors in tests/src before this change).
**Written but NOT run:** the integration subset (catalog, entitlements, `test_grpc.py` now expects 13 limits and `think_longer_per_day == 10`). `make test-up` starts new containers, which I did not do. Run `pytest tests/integration/test_grpc.py tests/integration/test_catalog.py` before relying on it. Not run: `make test-all`.

**What changed**
- B1: `Limits.think_longer_per_day` (default 0, so a plan row or Valkey cache entry stored before it reads as 0; tested for both). plans.yaml: free 0, pro 10, max 30, org_pro 0 (both variants), org_max 20 (both variants), payg 0; `catalog_version` is `2026-11-22-think-longer`. The YAML loader also treats a missing `think_longer_per_day` as 0 (`OPTIONAL_LIMITS`), so a catalog published before this change still parses. `GetEntitlements` carries it through the generic limits map (no proto change). billing does not count think messages; `contracts/conversation-service.md` says conversation-service does.
- B2: `ModelEntry.think`; plans.yaml `think:` per model (default false); new column `billing.models.think` (migration `0023_model_think_flag`); `GET /billing/v1/models` and `/me/models` return `think` on every model.
- B3: `ChatResponseGenerated` accepts `think: bool = False` (schema 2.3); stored nowhere, rating unchanged. Contracts updated.

**Model flags (check these):** I set `think: true` for all ten offered models (gpt-oss 20b/120b on Groq, Haiku/Sonnet/Opus 5.5, GPT-6 Luna and 6.1 Sol, Gemini 3.1 Flash-Lite, 3.5 Flash-Lite, 3.8 Flash). I could not confirm each provider's control from here; llm-service decides what it really sends and its catalog check will flag a mismatch.

**`make catalog-sync` on the dev database:** `make migrate` ran (0023 applied). `make catalog-sync` succeeded but used the **published draft** `e2e-down-1795272514` (published 2026-11-21, a leftover e2e catalog), which wins over plans.yaml. So it created new plan versions (free v5, pro v7, max v5, org v3, payg v4) with `think_longer_per_day` 0 and left `models.think` false ("models changed: 0"); plans.yaml's new values are NOT live on dev yet. To apply them: `PAYMENT_GATEWAY=fake uv run python -m billing.cli catalog sync --yaml` (adds another version per plan; I did not run it because it overrides a published catalog).

**Decisions for you**
- Think limit values and PAYG stay as in the contract; confirm against the Max margin.
- Whether to force `--yaml` on dev, or publish a real draft.
- Nothing pushed.
