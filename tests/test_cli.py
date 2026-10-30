"""The command line, on the small store copied into a world of its own."""

from __future__ import annotations

import json
import shutil

import pytest
from typer.testing import CliRunner

from devprod import settings
from devprod.cli import app
from tests.conftest import add_failed_run, add_pr

runner = CliRunner()


@pytest.fixture
def mini(store):
    add_pr(store, 1, ["schemas"])
    add_failed_run(store, 7, 1, "api", "FAILED services/api/tests/test_x.py::test_3 - AssertionError")
    store.commit()
    dest = settings.observed("mini")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(store.path, dest)
    yield "mini"
    shutil.rmtree(dest.parent)


def ok(*args: str) -> str:
    r = runner.invoke(app, list(args))
    assert r.exit_code == 0, r.output
    return r.output


def test_triage_prints_the_comment(mini):
    out = ok("triage", "--run", "7", "--world", mini)
    assert "**CI triage: regression**" in out and "api::test_3" in out
    assert runner.invoke(app, ["triage", "--run", "99", "--world", mini]).exit_code == 2


def test_missing_world_says_how_to_build_it():
    r = runner.invoke(app, ["what-runs", "a.py", "--world", "never-built"])
    assert r.exit_code == 2 and "devprod run" in r.output


def test_what_runs_and_shards(mini):
    assert ok("what-runs", "libs/schemas/x.py", "--world", mini).split() == ["ci", "/", "api", "ci", "/", "schemas"]
    shards = json.loads(ok("plan-shards", "api", "--target-minutes", "0.5", "--world", mini))
    assert shards == list(range(len(shards))) and len(shards) > 1
    tests = ok("shard-tests", "api", "--shard", "0", "--target-minutes", "0.5", "--world", mini).split()
    assert tests and all(t.startswith("api::") for t in tests)
    assert runner.invoke(app, ["shard-tests", "api", "--shard", "99", "--world", mini]).exit_code == 2


def test_workflows_write_then_check(mini, tmp_path):
    assert "wrote 8 files" in ok("workflows", "--out", str(tmp_path), "--world", mini)
    assert "up to date" in ok("workflows", "--out", str(tmp_path), "--check", "--world", mini)
    (tmp_path / ".github" / "workflows" / "api.yml").write_text("edited by hand")
    assert runner.invoke(app, ["workflows", "--out", str(tmp_path), "--check", "--world", mini]).exit_code == 1


def test_new_module_then_test_and_invoke_it(mini, tmp_path):
    out = ok("new", "pricing-sim", "--dep", "core", "--root", str(tmp_path), "--world", mini)
    assert "services/pricing-sim/template.yaml" in out
    base = tmp_path / "services" / "pricing-sim"
    r = runner.invoke(app, ["dev", "test", "pricing-sim", "--root", str(tmp_path)])
    assert r.exit_code == 0
    assert '"statusCode": 200' in ok("dev", "invoke", str(base), str(base / "events" / "sample.json"))
    again = runner.invoke(app, ["new", "pricing-sim", "--root", str(tmp_path), "--world", mini])
    assert again.exit_code == 2
    dry = ok("new", "charts", "--language", "typescript", "--dry-run", "--world", mini)
    assert "frontend/charts/package.json" in dry


def test_gate_pr_dry_run(mini, tmp_path, monkeypatch):
    import subprocess

    def git(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "base")
    (tmp_path / "libs" / "core").mkdir(parents=True)
    (tmp_path / "libs" / "core" / "a.py").write_text("x = 1\n")
    git("add", ".")
    git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "-m", "change")
    monkeypatch.chdir(tmp_path)
    out = ok("gate-pr", "--base", "HEAD~1", "--head", "HEAD", "--dry-run", "--world", mini)
    assert "ci / core" in out and "ci / api" in out and "ci / web" not in out
