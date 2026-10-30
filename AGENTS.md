# Working in this repository

For people and coding agents alike.

## Fast feedback

| What | Command | Time |
|---|---|---|
| Fast tests | `uv run pytest -q -m "not slow"` | ~15 s |
| Everything, with the pipeline | `uv run pytest -q` | ~30 s |
| Lint and format | `uv run ruff check src tests infra && uv run ruff format --check src tests` | 1 s |
| Both worlds, reports, release gate | `uv run devprod run` | ~15 s |
| Dashboard data | `uv run devprod export-web` | 2 s |
| Dashboard | `cd web && npm test && npx playwright test` | ~20 s |
| CloudFormation | `uv run cfn-lint infra/control-plane/template.yaml infra/accounts/deploy-role.yaml` | 5 s |

Terraform (`infra/runners`) is validated in CI only (`terraform fmt -check`, `validate`).

## Invariants (each has a test or a CI check)

1. **The platform never sees the truth.** Shipping code (`agent/`, `ci/`, `flaky/`, `deploy/`,
   `metrics/`, `paved/`, `service/`, `store.py`) decides from what GitHub, the CI, AWS and the cloud
   bill record. Only `devprod.sim`, `devprod.evaluation`, `devprod.export` and `devprod.heldout` may
   read `config/org.yaml` or `build/<world>/truth`. `tests/test_isolation.py` scans the imports;
   `tests/test_pipeline.py` poisons the truth and checks the metrics do not move.
2. **Thresholds live in `config/`**, not in code: CI policies (`ci.yaml`), flake quarantine
   (`flaky.yaml`), waves and gates (`deploy.yaml`), what the agent may do alone (`agent.yaml`), the
   release gate (`release.yaml`).
3. **The agent proposes; people decide what is irreversible.** It may rerun failed jobs once, comment
   and propose a quarantine. Quarantine on main, fix PRs and reverts need a person; the PreToolUse
   guard in `agent/claude.py` enforces this, not the prompt.
4. **Quarantine never hides a regression.** A quarantined test still runs; a test is not quarantined
   in a module the change touches; the release gate fails if a regression was hidden.
5. **A rollout's alpha is per rollout**, split over waves, tiers and looks. Adding a wave must not
   add false rollbacks.
6. **Generated files are generated**: `reports/`, `generated/.github/`, `web/public/data/`. CI fails
   if they differ from what the code produces.

## Held-out sets

`heldout/<set>/cases.yaml` are CI failures written blind, from `heldout/WRITING_FAILURES.md` only,
by someone who has not seen the code. Run a set once (`devprod heldout <set>`); its first run in
`reports/heldout/<set>/first_run.json` is never rewritten. Never change the agent to pass a set. If
you study a set's failures, write `regression` to `heldout/<set>/STATUS` first, and have a new set
written blind.

## Where things are

- `src/devprod/sim/` the simulated organisation (truth): repository, pull requests, CI, outages, customers
- `src/devprod/ci/` module graph, sharding, runner fleet and scaler, workflow generator
- `src/devprod/flaky/` flip-based flake register
- `src/devprod/agent/` triage tools, the rule-based stand-in, the Claude Agent SDK runner
- `src/devprod/deploy/` rollout state machine, AWS adapters, the DynamoDB worker
- `src/devprod/service/` webhook API, webhook Lambda, MCP server for engineers' agents
- `src/devprod/paved/` `devprod new` and the local Lambda loop
- `infra/` runner fleet (Terraform), control plane (SAM), customer-account role (CloudFormation)
- `web/` the dashboard (React 19, motion, Vite)
