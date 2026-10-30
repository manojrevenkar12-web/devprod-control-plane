"""The whole thing: both worlds, the evaluation, the reports and the gate (builds once, ~20 s)."""

from __future__ import annotations

import json
import shutil

import pytest
from mcp.client.client import Client

from devprod import evaluation, pipeline, settings
from devprod.metrics import devprod
from devprod.sim import world
from devprod.store import Store

pytestmark = pytest.mark.slow


@pytest.fixture(scope="session")
def results():
    assert "test" in str(settings.BUILD), "pipeline tests must not overwrite the committed build"
    return pipeline.run(log=lambda *_: None)


def test_gate_passes(results):
    failed = [c for c in results["gate"] if not c["passed"]]
    assert not failed, failed


def test_reports_are_written(results):
    text = (settings.REPORTS / "RESULTS.md").read_text()
    assert "## Release gate" in text and "| Truth \\ agent |" in text and "Ablation" in text
    assert json.loads((settings.REPORTS / "results.json").read_text())["gate"] == results["gate"]


def test_both_worlds_face_the_same_changes(results):
    a = json.loads((settings.truth("baseline") / "prs.json").read_text())
    b = json.loads((settings.truth("control-plane") / "prs.json").read_text())
    assert [(p["pr"], p["regression"], p["covered"]) for p in a] == [
        (p["pr"], p["regression"], p["covered"]) for p in b
    ]
    assert json.loads((settings.truth("baseline") / "tests.json").read_text()) == json.loads(
        (settings.truth("control-plane") / "tests.json").read_text()
    )


def test_the_control_plane_catches_what_the_baseline_misses(results):
    b, c = (results["worlds"][p]["truth"]["ci"] for p in settings.POLICIES)
    assert c["regressions_caught"] > b["regressions_caught"]
    assert c["regressions_hidden_by_quarantine"] == 0
    assert c["flake_detector"]["wrongly_quarantined"] == []


def test_simulation_is_deterministic(results):
    before = json.loads((settings.truth("baseline") / "summary.json").read_text())
    assert world.World("baseline").run() == before


def test_metrics_do_not_change_when_the_truth_is_poisoned(results, tmp_path):
    store = Store(settings.observed("control-plane"))
    clean = devprod.compute(store)
    truth = settings.truth("control-plane")
    backup = tmp_path / "truth"
    shutil.copytree(truth, backup)
    try:
        for f in truth.glob("*.json"):
            f.write_text('{"poisoned": true}')
        assert devprod.compute(Store(settings.observed("control-plane"))) == clean
    finally:
        shutil.rmtree(truth)
        shutil.copytree(backup, truth)
    assert evaluation.ci("control-plane")["regressions"] > 0


async def test_mcp_server_over_the_built_world(results):
    from devprod.service.mcp_server import build

    failed = Store(settings.observed("control-plane")).rows(
        "select run_id from runs where conclusion='failure' order by run_id limit 1"
    )[0]["run_id"]
    async with Client(build(settings.observed("control-plane"))) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert {"explain_run", "what_will_run", "quarantined_tests", "ci_metrics", "plan_new_module"} <= names
        out = json.loads((await c.call_tool("explain_run", {"run_id": failed})).content[0].text)
        assert out["kind"] in ("infra", "dependency", "flaky", "regression") and out["evidence"]
        r = await c.call_tool("quarantined_tests", {})
        q = r.structured_content["result"] if r.structured_content else [json.loads(x.text) for x in r.content]
        assert q and all(row["owner"] for row in q)
        plan = json.loads(
            (await c.call_tool("plan_new_module", {"name": "pricing-sim", "deps": ["core"]})).content[0].text
        )
        assert "services/pricing-sim/template.yaml" in plan["files"]


def test_gate_fails_when_a_bound_is_broken(results):
    broken = json.loads(json.dumps(results))
    broken["worlds"]["control-plane"]["truth"]["ci"]["regressions_hidden_by_quarantine"] = 1
    assert not next(c for c in pipeline.gate(broken) if c["name"] == "quarantine never hides a regression")["passed"]
