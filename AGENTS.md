# billing-service: agent instructions

Read `PHASES.md` for the build plan, `docs/hld.md` and `docs/lld.md` for the design.

- **Source of truth.** When the docs disagree: the LLD's Decisions section wins, then the rest of the LLD, then the HLD. If a conflict still affects what you build, stop and ask rather than guess.
- **One phase per session or pull request.** Stop at that phase's exit check. Do not start the next phase.
- **Never edit the docs** in `docs/`. Report errors you find instead.
- Follow the layer rules (domain is pure; app depends on ports only). import-linter must pass.
- Money is integer paise or micro-rupees, never `float`. Time comes from the injected `Clock`.
- No live third-party calls in tests (Razorpay, LLM providers). Use the fake `PaymentGateway`; real Postgres, Valkey and Kafka via Testcontainers.
