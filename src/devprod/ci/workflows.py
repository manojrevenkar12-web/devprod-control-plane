"""GitHub Actions workflows generated per module, so CI in the monorepo stays consistent as modules are
added, and no one hand-edits 25 near-identical files.

Each module gets one workflow. It triggers on its own path and the paths of everything it depends on
(a change to `libs/core` runs every module that uses core), shards its tests by recorded duration into
a matrix, keys its dependency cache on the lockfile and its own manifest, runs on the self-hosted fleet
with labels for the language, and cancels superseded runs of the same branch. On failure it hands the
run to the triage agent.

Path filters have one catch: a required check whose workflow was skipped never reports, and the pull
request cannot merge. So there is one more workflow, `ci-gate`, which always runs, works out which
module workflows the change should have started, waits for them through the REST API, and is the only
required check."""

from __future__ import annotations

from typing import Any

import yaml

from .. import settings

ACTIONS = {  # pinned majors; Dependabot keeps them current
    "checkout": "actions/checkout@v5",
    "setup-uv": "astral-sh/setup-uv@v6",
    "setup-node": "actions/setup-node@v5",
    "pnpm": "pnpm/action-setup@v4",
    "upload": "actions/upload-artifact@v4",
}


def depends_on(module: str, modules: dict[str, dict[str, Any]]) -> list[str]:
    """Every module `module` depends on, transitively, in a stable order."""
    out: list[str] = []
    todo = list(modules[module]["deps"])
    while todo:
        m = todo.pop(0)
        if m in out or m not in modules:
            continue
        out.append(m)
        todo.extend(modules[m]["deps"])
    return sorted(out)


def paths(module: str, modules: dict[str, dict[str, Any]]) -> list[str]:
    own = modules[module]["path"]
    return [
        f"{own}/**",
        *(f"{modules[d]['path']}/**" for d in depends_on(module, modules)),
        f".github/workflows/{module}.yml",
    ]


def _labels(language: str, size: str = "medium") -> list[str]:
    return ["self-hosted", "linux", "arm64", f"ci-{language}", size]


def workflow(module: str, modules: dict[str, dict[str, Any]]) -> dict[str, Any]:
    m = modules[module]
    cfg = settings.load("ci")["control-plane"]
    lang = m["language"]
    plan = {
        "runs-on": _labels(lang, "small"),
        "outputs": {"shards": "${{ steps.plan.outputs.shards }}"},
        "steps": [
            {"uses": ACTIONS["checkout"]},
            {"uses": ACTIONS["setup-uv"], "with": {"enable-cache": True}},
            {
                "id": "plan",
                "run": f'echo "shards=$(uvx --from ./tools/devprod devprod plan-shards {module} '
                f'--target-minutes {cfg["shard_target_minutes"]} --max {cfg["max_shards"]})" '
                '>> "$GITHUB_OUTPUT"',
            },
        ],
    }
    if lang == "python":
        setup: list[dict[str, Any]] = [
            {
                "uses": ACTIONS["setup-uv"],
                "with": {
                    "enable-cache": True,
                    "cache-dependency-glob": f"uv.lock\n{m['path']}/pyproject.toml",
                    "cache-suffix": module,
                },
            },
            {"run": f"uv sync --frozen --package {module}"},
        ]
        run = (
            f"uvx --from ./tools/devprod devprod shard-tests {module} --shard ${{{{ matrix.shard }}}} > tests.txt\n"
            f"uv run --package {module} pytest -q -p no:cacheprovider --junitxml=junit.xml $(cat tests.txt)"
        )
    else:
        setup = [
            {"uses": ACTIONS["pnpm"]},
            {"uses": ACTIONS["setup-node"], "with": {"node-version": 22, "cache": "pnpm"}},
            {"run": f"pnpm install --frozen-lockfile --filter {module}..."},
        ]
        run = (
            f"pnpm --filter {module} exec vitest run --reporter=junit --outputFile=junit.xml "
            f"--shard=$(( ${{{{ matrix.shard }}}} + 1 ))/${{{{ strategy.job-total }}}}"
        )
    test = {
        "needs": "plan",
        "runs-on": _labels(lang),
        "timeout-minutes": 30,
        "strategy": {"fail-fast": False, "matrix": {"shard": "${{ fromJSON(needs.plan.outputs.shards) }}"}},
        "steps": [
            {"uses": ACTIONS["checkout"]},
            *setup,
            {"name": "Test", "run": run},
            {
                "if": "always()",
                "uses": ACTIONS["upload"],
                "with": {"name": f"junit-{module}-${{{{ matrix.shard }}}}", "path": "junit.xml", "retention-days": 14},
            },
        ],
    }
    triage = {
        "needs": "test",
        "if": "failure()",
        "runs-on": _labels(lang, "small"),
        "permissions": {"actions": "write", "pull-requests": "write", "checks": "read"},
        "steps": [
            {"uses": ACTIONS["checkout"]},
            {"uses": ACTIONS["setup-uv"]},
            {
                "run": "uvx --from ./tools/devprod devprod triage --run ${{ github.run_id }} "
                "--pr ${{ github.event.pull_request.number }}",
                "env": {
                    "GITHUB_TOKEN": "${{ secrets.GITHUB_TOKEN }}",
                    "ANTHROPIC_API_KEY": "${{ secrets.ANTHROPIC_API_KEY }}",
                    "DEVPROD_AGENT": "claude",
                },
            },
        ],
    }
    return {
        "name": f"ci / {module}",
        "on": {"pull_request": {"paths": paths(module, modules)}, "merge_group": {}},
        "permissions": {"contents": "read"},
        "concurrency": {"group": f"ci-{module}-${{{{ github.head_ref || github.ref }}}}", "cancel-in-progress": True},
        "jobs": {"plan": plan, "test": test, "triage": triage},
    }


def gate_workflow() -> dict[str, Any]:
    return {
        "name": "ci-gate",
        "on": {"pull_request": {}, "merge_group": {}},
        "permissions": {"contents": "read", "checks": "read", "actions": "read"},
        "concurrency": {"group": "ci-gate-${{ github.head_ref || github.ref }}", "cancel-in-progress": True},
        "jobs": {
            "gate": {
                "runs-on": _labels("python", "small"),
                "timeout-minutes": 90,
                "steps": [
                    {"uses": ACTIONS["checkout"], "with": {"fetch-depth": 0}},
                    {"uses": ACTIONS["setup-uv"]},
                    {
                        "name": "Wait for the module workflows this change needs",
                        "run": "uvx --from ./tools/devprod devprod gate-pr --base ${{ github.event.pull_request.base.sha "
                        "|| github.event.merge_group.base_sha }} --head ${{ github.sha }}",
                        "env": {"GITHUB_TOKEN": "${{ secrets.GITHUB_TOKEN }}"},
                    },
                ],
            }
        },
    }


class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper: yaml.SafeDumper, s: str) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None)


_Dumper.add_representer(str, _str)


def dump(doc: dict[str, Any]) -> str:
    # `on` stays quoted: YAML 1.1 reads a bare on as true
    head = "# Generated by `devprod workflows`. Do not edit; change src/devprod/ci/workflows.py.\n"
    return head + yaml.dump(doc, Dumper=_Dumper, sort_keys=False, width=200)


def workflow_for(module: str, modules: dict[str, dict[str, Any]]) -> str:
    return dump(workflow(module, modules))


def all_files(modules: dict[str, dict[str, Any]]) -> dict[str, str]:
    files = {f".github/workflows/{m}.yml": workflow_for(m, modules) for m in sorted(modules)}
    files[".github/workflows/ci-gate.yml"] = dump(gate_workflow())
    files[".github/CODEOWNERS"] = codeowners(modules)
    return files


def codeowners(modules: dict[str, dict[str, Any]]) -> str:
    lines = ["# Generated by `devprod workflows`.", "/.github/workflows/ @org/platform"]
    lines += [f"/{m['path']}/ @org/{m['owner']}" for _, m in sorted(modules.items(), key=lambda x: x[1]["path"])]
    return "\n".join(lines) + "\n"


def required_workflows(changed_files: list[str], modules: dict[str, dict[str, Any]]) -> list[str]:
    """What ci-gate waits for: the module workflows whose path filters match the change (GitHub's own
    rule), so the gate and the filters cannot disagree."""

    def match(f: str, p: str) -> bool:
        return f.startswith(p[:-2]) if p.endswith("/**") else f == p

    return [f"ci / {m}" for m in sorted(modules) if any(match(f, p) for f in changed_files for p in paths(m, modules))]
