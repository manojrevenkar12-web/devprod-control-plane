"""The Claude Agent SDK wiring, without a model: options, tools, the guard and the answer schema."""

from __future__ import annotations

import json

from devprod.agent import claude
from devprod.agent.tools import Tools
from devprod.agent.triage import KINDS
from devprod.ci.graph import Graph
from devprod.flaky.detector import FlakeRegister
from tests.conftest import add_failed_run, add_pr


def tools(store) -> Tools:
    return Tools(store, Graph(store.modules()), FlakeRegister(store, {}))


def test_options_give_no_built_in_tools_and_a_budget(store):
    o = claude.options(tools(store), 1, 100.0)
    assert o.tools == [] and o.permission_mode == "dontAsk" and o.setting_sources == []
    assert o.max_budget_usd == 0.40 and o.max_turns == 12
    assert set(o.agents) == {"log-reader", "history-checker"}
    assert all(t.startswith("mcp__devprod__") or t in ("Task", "Agent") for t in o.allowed_tools)
    assert "mcp__devprod__revert" not in o.allowed_tools
    assert o.output_format["schema"]["properties"]["kind"]["enum"] == list(KINDS)
    assert o.hooks["PreToolUse"][0].hooks


def test_subagents_only_read(store):
    for a in claude.subagents().values():
        assert a.model == "haiku"
        assert all(t.removeprefix("mcp__devprod__") in claude.READS for t in a.tools)


async def test_guard_allows_reads_one_rerun_and_denies_the_rest():
    g = claude.Guard()
    ok = await g({"tool_name": "mcp__devprod__failed_jobs", "tool_input": {}}, None, None)
    assert ok == {}
    assert await g({"tool_name": "mcp__devprod__rerun_failed_jobs", "tool_input": {}}, None, None) == {}
    for name in (
        "mcp__devprod__rerun_failed_jobs",
        "mcp__devprod__revert",
        "mcp__devprod__open_fix_pr",
        "Bash",
        "WebFetch",
        "mcp__other__thing",
    ):
        out = await g({"tool_name": name, "tool_input": {}}, None, None)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", name
    assert len(g.denied) == 6 and "needs a person" in g.denied[1]["reason"]


async def test_tools_answer_with_the_ci_record(store):
    add_pr(store, 1, ["schemas"])
    add_failed_run(store, 1, 1, "api", "FAILED services/api/tests/test_x.py::test_3 - AssertionError")
    t = tools(store)
    by = {x.name: x for x in claude.build_tools(t, 1, 100.0)}
    jobs = json.loads((await by["failed_jobs"].handler({"run_id": 1}))["content"][0]["text"])
    assert jobs[0]["module"] == "api"
    tests = json.loads(
        (await by["failed_tests"].handler({"log": jobs[0]["log_tail"], "module": "api"}))["content"][0]["text"]
    )
    assert tests == ["api::test_3"]
    reach = json.loads((await by["pr_changes"].handler({"pr": 1}))["content"][0]["text"])
    assert "api" in reach["affected"]
    await by["comment"].handler({"body": "hi", "classification": "flaky", "run_id": 1})
    await by["propose_quarantine"].handler({"test_id": "api::test_3", "evidence": "3 flips"})
    assert store.rows("select author, classification from comments") == [
        {"author": "triage-agent", "classification": "flaky"}
    ]
    assert t.actions[-1]["action"] == "propose_quarantine"
    assert all(x.annotations is not None for x in by.values())
    assert by["failed_jobs"].annotations.read_only_hint and not by["comment"].annotations.read_only_hint


def test_parse_accepts_only_schema_answers():
    good = {"kind": "regression", "confidence": 0.9, "tests": ["a::b"], "evidence": ["x"], "next_step": "tell_author"}
    assert claude.parse(good).kind == "regression"
    assert claude.parse(json.dumps(good)).next_step == "tell_author"
    for bad in (None, "not json", {"kind": "cosmic-ray"}, {**good, "next_step": "rm -rf"}):
        d = claude.parse(bad)
        assert d.confidence == 0.0 and d.next_step == "tell_author"


def test_prompt_says_what_the_confirming_rerun_did():
    assert "failed again" in claude.prompt(1, 2, True)
    assert "passed" in claude.prompt(1, 2, False)
    assert "confirming" not in claude.prompt(1, 2, None)
