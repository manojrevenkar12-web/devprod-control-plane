"""Generated workflows, the ci-gate rule, the scaffold and the local inner loop."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from devprod.ci import workflows
from devprod.paved import local, scaffold


def test_workflow_triggers_on_its_dependencies_and_shards(store):
    mods = store.modules()
    doc = yaml.safe_load(workflows.workflow_for("api", mods))
    paths = doc["on"]["pull_request"]["paths"]
    assert paths == ["services/api/**", "libs/core/**", "libs/schemas/**", ".github/workflows/api.yml"]
    test = doc["jobs"]["test"]
    assert test["strategy"]["matrix"]["shard"] == "${{ fromJSON(needs.plan.outputs.shards) }}"
    assert test["strategy"]["fail-fast"] is False and "self-hosted" in test["runs-on"]
    assert doc["concurrency"]["cancel-in-progress"] is True
    assert doc["permissions"] == {"contents": "read"}
    assert doc["jobs"]["triage"]["if"] == "failure()"
    steps = [s.get("uses", "") for s in test["steps"]]
    assert all("@v" in s for s in steps if s)  # pinned


def test_typescript_workflow_uses_pnpm_and_vitest_shards(store):
    doc = yaml.safe_load(workflows.workflow_for("web", store.modules()))
    run = next(s["run"] for s in doc["jobs"]["test"]["steps"] if s.get("name") == "Test")
    assert "vitest run" in run and "--shard=" in run
    assert doc["on"]["pull_request"]["paths"][:2] == ["frontend/web/**", "frontend/ui-kit/**"]


def test_generated_yaml_keeps_on_as_a_key(store):
    text = workflows.workflow_for("api", store.modules())
    assert "'on':" in text and "true:" not in text
    assert "run: |" in text  # multi-line scripts stay readable


def test_ci_gate_waits_for_exactly_what_the_filters_start(store):
    mods = store.modules()
    assert workflows.required_workflows(["libs/core/a.py"], mods) == [
        "ci / api",
        "ci / billing",
        "ci / core",
        "ci / schemas",
    ]
    assert workflows.required_workflows(["frontend/web/x.tsx"], mods) == ["ci / web"]
    assert workflows.required_workflows([".github/workflows/billing.yml"], mods) == ["ci / billing"]
    assert workflows.required_workflows(["README.md"], mods) == []
    files = workflows.all_files(mods)
    assert ".github/workflows/ci-gate.yml" in files and len(files) == len(mods) + 2
    assert "/libs/core/ @org/platform" in files[".github/CODEOWNERS"]


def test_scaffold_refuses_bad_input(store):
    mods = store.modules()
    for name, lang, deps in [
        ("Bad_Name", "python", []),
        ("api", "python", []),
        ("ok-name", "rust", []),
        ("ok-name", "python", ["nope"]),
    ]:
        with pytest.raises(scaffold.ScaffoldError):
            scaffold.render(name, lang, deps, mods)


def test_scaffold_writes_a_module_that_tests_green_and_invokes_locally(store, tmp_path):
    files = scaffold.render("pricing-sim", "python", ["core"], store.modules(), owner="decisions")
    written = scaffold.write(tmp_path, files)
    assert "services/pricing-sim/template.yaml" in written and ".github/workflows/pricing-sim.yml" in written
    base = tmp_path / "services" / "pricing-sim"
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(base / "tests")],
        env={"PYTHONPATH": str(base / "src"), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    out = local.invoke(base, base / "events" / "sample.json")
    assert out["ok"] and out["result"]["statusCode"] == 200
    assert "pricing-sim" in out["result"]["body"]
    with pytest.raises(scaffold.ScaffoldError):
        scaffold.write(tmp_path, files)  # never overwrites
    assert "@org/decisions" in (tmp_path / ".github" / "CODEOWNERS").read_text()


def test_local_invoke_reports_a_broken_handler(tmp_path):
    (tmp_path / "src" / "svc").mkdir(parents=True)
    (tmp_path / "src" / "svc" / "__init__.py").write_text("")
    (tmp_path / "src" / "svc" / "handler.py").write_text("def handler(e, c):\n    raise KeyError('body')\n")
    (tmp_path / "template.yaml").write_text("Resources:\n  F:\n    Properties:\n      Handler: svc.handler.handler\n")
    (tmp_path / "e.json").write_text("{}")
    out = local.invoke(tmp_path, tmp_path / "e.json")
    assert not out["ok"] and "KeyError" in out["error"]


def problems(text: str) -> list[str]:
    """cfn-lint errors and warnings (the CLI's default; informational rules are left out)."""
    from cfnlint.api import lint_all

    return [str(m) for m in lint_all(text) if m.rule.id[0] in "EW"]


def test_scaffolded_sam_template_passes_cfn_lint(store, tmp_path):
    text = scaffold.render("pricing-sim", "python", [], store.modules())["services/pricing-sim/template.yaml"]
    assert problems(text) == []


@pytest.mark.parametrize("path", ["infra/control-plane/template.yaml", "infra/accounts/deploy-role.yaml"])
def test_infra_templates_pass_cfn_lint(path):
    assert problems((Path(__file__).parents[1] / path).read_text()) == []


def test_typescript_scaffold(store):
    files = scaffold.render("charts", "typescript", ["ui-kit"], store.modules())
    assert '"@org/ui-kit": "workspace:*"' in files["frontend/charts/package.json"]
    assert "vitest" in files["frontend/charts/src/index.test.ts"]
