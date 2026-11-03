# v1 launch checklist

Stage E extends this file. The items below are already known launch blockers.

## Launch blockers

- [ ] **Load gate green: p99 < 10 ms, 0 errors, at 200 reserves/s for 2 minutes, on a quiet
      Linux machine or in CI.** It was not met on the Windows development machine (Stage A, see
      `reports/stage-A.md`): the Phase 5 commit fails the same way there, so the cause is the
      machine, not the code. Do not launch on an unmeasured hot path.
- [ ] Razorpay test-mode run done (`runbooks/razorpay-test-mode.md`), recorded payloads saved
      under `tests/fixtures/razorpay/recorded/` and the tests passing on them.

## CI/CD (later)

Development comes first, so none of this is automated yet and `.github/workflows/` is untouched.
Before launch these checks should become a pipeline. Today each runs locally:

| Check | Local command | Stage |
| --- | --- | --- |
| Lint, types, import rules | `make lint` | every stage |
| Unit, property and integration tests (Docker) | `make test` | every stage |
| `buf lint` and `buf breaking` for `billing.proto` | `make proto-check` | B |
| Generated protobuf code is current | `make proto && git diff --exit-code src/billing/gen` | B |
| Dependency vulnerability audit | `make audit` | E |
| Alert rules unit test | `make alerts-check` | E |
| Load gate (see above) | `make loadtest` on a quiet Linux machine | launch |
| Container image build and scan | `docker build -t billing-service .` | launch |
| Existing `ci.yml` | already in the repo; extend it rather than replace it | launch |
