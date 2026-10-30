# Writing held-out CI failures

This guide is all a writer of a held-out set may read. Do not read `src/`, `tests/`, `config/` or
anything under `build/` or `reports/`: the point of the set is to find out how the triage agent does on
failures written by someone who has not seen how it works.

## What the agent does

When a pull request's CI run fails, the triage agent says why, in one of four words, and what should
happen next:

| Kind | Meaning | Right next step |
|---|---|---|
| `infra` | The runner or the network failed, not the code: the machine ran out of memory or disk, was shut down or lost its connection, a checkout or cache download failed. | `rerun_failed_jobs` |
| `dependency` | Installing packages failed because a registry or an upstream release is broken. It usually hits many pull requests in the same hour. | `wait_and_rerun` |
| `flaky` | A test failed that sometimes fails on its own, or that the change cannot have affected. Rerunning the same commit would pass. | `rerun_failed_jobs` (or `propose_quarantine` once a confirming rerun passed) |
| `regression` | The change broke a test it can reach. | first `rerun_to_confirm`; after the confirming rerun failed again, `tell_author` |

The agent sees what a person on the platform team would see: the failed jobs' logs (the last few
thousand characters), which modules and files the pull request changes, which modules depend on them,
each failing test's recent history on the same job (how often it ran, failed, and *flipped*: failed and
then passed on the same commit), whether a test is quarantined, and how many pull requests failed at
the install step in the last two hours.

## The repository

A monorepo of 25 modules. A module's tests live under its path (`tests/test_*.py` for Python with
pytest; `src/**/*.test.tsx` for TypeScript with Vitest or Jest). A change to a module reaches that
module and every module that depends on it, directly or not.

| Module | Language | Path | Depends on | Owner |
|---|---|---|---|---|
| `core` | python | `libs/core` | – | platform |
| `schemas` | python | `libs/schemas` | core | platform |
| `decision-runtime` | python | `libs/decision-runtime` | core, schemas | decisions |
| `rules-engine` | python | `services/rules-engine` | auth, decision-runtime | decisions |
| `flow-editor-api` | python | `services/flow-editor-api` | auth, core, decision-runtime, schemas | decisions |
| `connectors-bureau` | python | `services/connectors-bureau` | core, decision-runtime, schemas | connectors |
| `connectors-banking` | python | `services/connectors-banking` | agent-runtime, auth, core, schemas | connectors |
| `connectors-kyc` | python | `services/connectors-kyc` | audit-log, core, decision-runtime, schemas | connectors |
| `agent-runtime` | python | `services/agent-runtime` | decision-runtime, schemas | decisions |
| `risk-models` | python | `services/risk-models` | agent-runtime, core, decision-runtime, flow-editor-api | risk |
| `decision-api` | python | `services/decision-api` | connectors-banking, core, decision-runtime | decisions |
| `audit-log` | python | `services/audit-log` | agent-runtime, core, schemas | platform |
| `data-sync` | python | `services/data-sync` | connectors-kyc, core, decision-runtime, schemas | data |
| `notifications` | python | `services/notifications` | core, flow-editor-api, schemas | platform |
| `billing` | python | `services/billing` | core, decision-runtime | data |
| `auth` | python | `services/auth` | agent-runtime, audit-log, core, decision-runtime, schemas | platform |
| `reporting` | python | `services/reporting` | core | data |
| `feature-store` | python | `services/feature-store` | billing, core, data-sync, schemas | risk |
| `ui-kit` | typescript | `frontend/ui-kit` | – | frontend |
| `web-app` | typescript | `frontend/web-app` | ui-kit, sdk-js | frontend |
| `flow-editor` | typescript | `frontend/flow-editor` | ui-kit | frontend |
| `admin-console` | typescript | `frontend/admin-console` | ui-kit | frontend |
| `docs-site` | typescript | `frontend/docs-site` | ui-kit | frontend |
| `analytics-ui` | typescript | `frontend/analytics-ui` | ui-kit | frontend |
| `sdk-js` | typescript | `frontend/sdk-js` | ui-kit | frontend |

Test ids are `<module>::<test function name>` for pytest (`decision-api::test_rejects_unknown_flow`)
and `<module>::<test name with spaces as underscores>` for JS (`web-app::renders_the_flow_list`).

## A case

Write each case as one YAML document item in `heldout/<set>/cases.yaml`:

```yaml
- id: oom-in-feature-store            # short, unique
  pr:
    modules: [billing]                # what the pull request changes
    files: [services/billing/src/billing/invoice.py]
  failed_jobs:                        # one or more
    - module: feature-store
      step: test                      # install | test
      log: |
        ...the last part of the job's log, as GitHub Actions would show it...
  history:                            # optional: the failing tests' history on this job before this run
    feature-store::test_joins_late_events: {runs: 140, fails: 6, flips: 5}
  quarantined: []                     # optional: tests already quarantined
  install_failures_last_2h: 0         # pull requests (including this one) that failed at install recently
  confirming_rerun: null              # null for a first look; failed | passed after the agent's confirming rerun
  expect:
    kind: infra
    next_step: rerun_failed_jobs
  why: the runner was OOM-killed mid-run; no test failed   # one line, for the reader of the results
```

## What makes a good set

- **Write logs the way real tools print them**: pytest (`FAILED path::test - Error`), pytest-xdist,
  Vitest, Jest, pip and uv resolver errors, npm and pnpm errors, the GitHub Actions runner's own
  messages, git and cache errors, Docker pulls, timeouts. Vary the details; do not copy one log and
  change a name. Include the noise a real tail has (warnings, timing lines, coverage tables).
- **Cover the hard cases**, not only the easy ones: a regression that looks like a timeout; a flaky
  test in a module the change reaches (history decides); a known-flaky test failing next to a real
  regression; an install failure that is really the change's own bad pin (one pull request, not many:
  that is a regression of the change, `kind: regression`); a runner death in the middle of test output;
  a test that failed once before but never flipped; a failure in a module the change cannot reach; a
  confirming rerun that passed; logs truncated at the start.
- About 40 cases, roughly a quarter of each kind, with at least eight that you would call hard.
- Each `expect` must be what a careful senior engineer would decide from the same information, and
  `why` says in one line what decides it.
