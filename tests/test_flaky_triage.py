"""The flaky-test register and the triage stand-in, on a small hand-made CI record."""

from __future__ import annotations

from devprod.agent.tools import Tools
from devprod.agent.triage import StandIn, as_dict, costs
from devprod.ci.graph import Graph
from devprod.flaky.detector import FlakeRegister, replay
from tests.conftest import add_failed_run, add_pr

PYTEST_LOG = """Run python tests for {m} (shard 0)
=================================== FAILURES ===================================
_____ test_3 _____
E       AssertionError: assert 4 == 5
=========================== short test summary info ============================
FAILED {path}/tests/test_x.py::test_3 - AssertionError: assert 4 == 5
=========== 1 failed, 39 passed in 12.00s ==========="""

JEST_LOG = """Run typescript tests for web (shard 0)
FAIL frontend/web/src/Flow.test.tsx
  ● renders flow

    thrown: "Exceeded timeout of 5000 ms for a test."
    ✕ renders flow (5003 ms)
Tests:       1 failed, 39 passed, 40 total"""


def owners(store):
    return {m: v["owner"] for m, v in store.modules().items()}


def test_a_flip_is_a_failure_that_passes_on_the_same_commit(store):
    r = FlakeRegister(store, owners(store))
    r.observe_job("a", "api#0", ["api::t1", "api::t2"], ["api::t1"])
    r.observe_job("a", "api#0", ["api::t1", "api::t2"], [])  # rerun passes: a flip
    r.observe_job("b", "api#0", ["api::t1", "api::t2"], ["api::t2"])
    r.observe_job("b", "api#0", ["api::t1", "api::t2"], ["api::t2"])  # fails again: broken, not flaky
    assert r.flips["api::t1"] == 1 and r.flips.get("api::t2", 0) == 0
    assert r.runs("api::t1") == 4 and r.fails["api::t2"] == 2
    assert r.p_flaky("api::t2") == 0.0


def test_quarantine_needs_repeated_flips_and_respects_touched_modules(store):
    r = FlakeRegister(store, owners(store))
    for k in range(3):
        r.observe_job(f"s{k}", "api#0", ["api::t1"], ["api::t1"])
        r.observe_job(f"s{k}", "api#0", ["api::t1"], [])
    assert r.should_quarantine("api::t1")
    assert r.review(10.0, touched_modules={"api"}) == {"quarantined": [], "graduated": []}
    out = r.review(10.0)
    assert out["quarantined"] == ["api::t1"]
    row = store.rows("select * from quarantine")[0]
    assert row["owner"] == "decisions" and row["until_ts"] > row["since_ts"] and "3 flips" in row["reason"]


def test_one_flip_is_not_enough(store):
    r = FlakeRegister(store, owners(store))
    r.observe_job("s", "api#0", ["api::t1"], ["api::t1"])
    r.observe_job("s", "api#0", ["api::t1"], [])
    assert not r.should_quarantine("api::t1")


def test_graduates_after_clean_runs(store):
    r = FlakeRegister(store, owners(store))
    for k in range(3):
        r.observe_job(f"s{k}", "api#0", ["api::t1"], ["api::t1"])
        r.observe_job(f"s{k}", "api#0", ["api::t1"], [])
    r.review(0.0)
    for k in range(r.cfg["graduate_after_clean_runs"]):
        r.observe_job(f"c{k}", "api#0", ["api::t1"], [])
    assert r.review(1.0)["graduated"] == ["api::t1"]
    assert store.quarantined() == set()


def test_replay_rebuilds_the_register_from_the_ci_record(store):
    add_pr(store, 1, ["api"])
    for attempt, (failed, conclusion) in enumerate([(["api::test_3"], "failure"), ([], "success")]):
        store.insert(
            "runs",
            {
                "run_id": attempt + 1,
                "pr": 1,
                "sha": "s1",
                "attempt": attempt + 1,
                "trigger": "push",
                "started_ts": attempt,
                "finished_ts": attempt + 0.5,
                "conclusion": conclusion,
            },
        )
        job = store.insert(
            "jobs",
            {
                "run_id": attempt + 1,
                "module": "api",
                "shard": 0,
                "runner": "r",
                "queued_ts": attempt,
                "started_ts": attempt,
                "finished_ts": attempt + 0.5,
                "conclusion": conclusion,
                "step_failed": "test" if failed else None,
                "minutes": 1,
                "cache_hit": 1,
            },
        )
        for t in failed:
            store.insert(
                "test_results",
                {
                    "job_id": job,
                    "run_id": attempt + 1,
                    "sha": "s1",
                    "test_id": t,
                    "outcome": "failed",
                    "seconds": 1,
                    "ts": attempt,
                },
            )
    h = replay(store).history("api::test_3")
    assert h["flips"] == 1 and h["fails"] == 1 and h["runs"] == 2


def tools(store) -> Tools:
    return Tools(store, Graph(store.modules()), FlakeRegister(store, owners(store)))


def test_parses_pytest_and_jest_failures(store):
    t = tools(store)
    assert t.failed_tests(PYTEST_LOG.format(m="api", path="services/api"), "api") == ["api::test_3"]
    assert t.failed_tests(JEST_LOG, "web") == ["web::renders_flow"]


def test_infra_marker_means_rerun(store):
    add_pr(store, 1, ["api"])
    add_failed_run(store, 1, 1, "api", "Run tests\nOSError: [Errno 28] No space left on device: '/tmp'")
    d = StandIn(tools(store)).diagnose(1, 1, 100)
    assert (d.kind, d.next_step) == ("infra", "rerun_failed_jobs")


def test_dependency_outage_across_prs_means_wait(store):
    for pr in (1, 2, 3, 4):
        add_pr(store, pr, ["api"])
        add_failed_run(
            store, pr, pr, "api", "ERROR: No matching distribution found for httpx==9.9", t=100 + pr, step="install"
        )
    d = StandIn(tools(store)).diagnose(4, 4, 105)
    assert d.kind == "dependency" and d.next_step == "wait_and_rerun" and d.confidence >= 0.9
    assert "4 pull requests failed at install" in d.evidence[1]


def test_reachable_failure_is_confirmed_before_blaming_the_author(store):
    add_pr(store, 1, ["schemas"])  # schemas reaches api
    add_failed_run(store, 1, 1, "api", PYTEST_LOG.format(m="api", path="services/api"))
    agent = StandIn(tools(store))
    first = agent.diagnose(1, 1, 100)
    assert (first.kind, first.next_step) == ("regression", "rerun_to_confirm")
    again = agent.diagnose(1, 1, 100, confirmed=True)
    assert (again.kind, again.next_step) == ("regression", "tell_author") and again.confidence > first.confidence
    passed = agent.diagnose(1, 1, 100, confirmed=False)
    assert (passed.kind, passed.next_step) == ("flaky", "propose_quarantine")
    assert "api::test_3" in again.body() and as_dict(again)["tests"] == ["api::test_3"]


def test_unreachable_failure_is_flaky(store):
    add_pr(store, 1, ["billing"])  # billing does not reach web
    add_failed_run(store, 1, 1, "web", JEST_LOG)
    d = StandIn(tools(store)).diagnose(1, 1, 100)
    assert d.kind == "flaky" and "cannot reach" in d.evidence[0]


def test_known_flaky_test_is_flaky_even_when_reachable(store):
    t = tools(store)
    for k in range(4):
        t.flakes.observe_job(f"s{k}", "api#0", ["api::test_3"], ["api::test_3"])
        t.flakes.observe_job(f"s{k}", "api#0", ["api::test_3"], [])
    add_pr(store, 1, ["api"])
    add_failed_run(store, 1, 1, "api", PYTEST_LOG.format(m="api", path="services/api"))
    d = StandIn(t).diagnose(1, 1, 100)
    assert d.kind == "flaky" and "flipped 4 times" in d.evidence[0]


def test_failure_without_tests_or_markers_is_a_low_confidence_rerun(store):
    add_pr(store, 1, ["api"])
    add_failed_run(store, 1, 1, "api", "Process completed with exit code 2.")
    d = StandIn(tools(store)).diagnose(1, 1, 100)
    assert d.kind == "infra" and d.confidence < 0.5
    assert costs(3) > 0
