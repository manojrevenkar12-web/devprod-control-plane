"""DevProd and DORA metrics from a hand-made record, so every number can be worked out by hand."""

from __future__ import annotations

from devprod.metrics import devprod
from tests.conftest import add_pr

DAY = 1440


def run(s, run_id, pr, sha, t0, t1, conclusion, trigger="push", queued=0.0):
    s.insert(
        "runs",
        {
            "run_id": run_id,
            "pr": pr,
            "sha": sha,
            "attempt": 1,
            "trigger": trigger,
            "started_ts": t0,
            "finished_ts": t1,
            "conclusion": conclusion,
        },
    )
    s.insert(
        "jobs",
        {
            "run_id": run_id,
            "module": "api",
            "shard": 0,
            "runner": "r",
            "queued_ts": t0 - queued,
            "started_ts": t0,
            "finished_ts": t1,
            "conclusion": conclusion,
            "step_failed": "test" if conclusion == "failure" else None,
            "minutes": t1 - t0,
            "cache_hit": 1,
        },
    )


def test_ci_metrics(store):
    add_pr(store, 1, ["api"])
    add_pr(store, 2, ["api"])
    store.update("prs", "pr", 1, opened_ts=600.0, merged_ts=600.0 + 120)  # 2 h
    store.update("prs", "pr", 2, opened_ts=700.0, merged_ts=700.0 + 360)  # 6 h
    run(store, 1, 1, "a", 600, 610, "failure", queued=2)
    run(store, 2, 1, "a", 612, 620, "success", trigger="rerun_failed")  # failed then passed: a flake as seen
    run(store, 3, 2, "b", 700, 712, "success")
    store.insert("fleet_usage", {"day": 0, "runner_minutes": 900, "peak_runners": 4, "queue_minutes_mean": None})
    store.insert("engineer_time", {"pr": 1, "run_id": 1, "ts": 611, "minutes": 15, "kind": "flaky"})
    m = devprod.ci(store)
    assert m["pr_lead_time_hours"] == {"p50": 4.0, "p90": 5.6}
    assert m["ci_wall_minutes"]["p50"] == 11.0  # push runs only: 10 and 12
    assert m["push_to_green_minutes"]["p50"] == 16.0  # (620 - 600) and (712 - 700)
    assert m["failed_then_passed_same_commit"] == 1 and m["failed_push_share"] == 0.5
    assert m["queue_minutes"]["mean"] == round(2 / 3, 2)
    assert m["runner_minutes"] == 900 and m["peak_runners"] == 4
    assert m["engineers"] == 2 and m["working_days"] == 1
    assert m["prs_per_engineer_day"] == 1.0 and m["engineer_minutes_on_failures"] == 15
    assert m["failed_jobs_by_step"] == {"test": 1}


def test_delivery_metrics_follow_dora(store):
    for pr, merged in ((1, 100.0), (2, 200.0), (3, 300.0)):
        store.insert("commits", {"sha": f"s{pr}", "pr": pr, "merged_ts": merged})
    store.insert("releases", {"release_id": 1, "ts": 150.0, "shas": ["s1"]})
    store.insert("releases", {"release_id": 2, "ts": 250.0, "shas": ["s2"]})
    store.insert("releases", {"release_id": 3, "ts": 350.0, "shas": ["s3"]})

    def dep(rel, acc, t, status, wave="all"):
        store.insert(
            "deployments",
            {
                "release_id": rel,
                "account_id": acc,
                "wave": wave,
                "started_ts": t - 5,
                "finished_ts": t,
                "status": status,
            },
        )

    dep(1, "internal-dev", 160, "completed")
    dep(1, "cust-1", 190, "completed")
    dep(1, "cust-2", 220, "completed")
    dep(2, "cust-1", 270, "rolled_back")  # reached a customer, then rolled back
    dep(2, "cust-1", 300, "rollback", wave="rollback")
    dep(3, "internal-dev", 360, "rolled_back")  # stopped before any customer: not a change failure
    m = devprod.delivery(store)
    assert m["rollouts_reaching_customers"] == 2 and m["completed_rollouts"] == 1
    assert m["change_failure_rate"] == 0.5
    assert m["time_to_restore_minutes"] == {"p50": 30.0, "max": 30.0}
    # s1 merged at 100, release 1 reached 95% of customers at ~218.5 (the only completed rollout)
    assert m["lead_time_for_changes_hours"]["p50"] == round((218.5 - 100) / 60, 1)
    assert m["rollbacks"] == 1 and m["customer_accounts_touched_by_failed_rollouts"] == 1


def test_working_days_skip_weekends():
    assert devprod.working_days(0, 6 * DAY) == 5  # Monday to Sunday
    assert devprod.working_days(5 * DAY, 6 * DAY + 10) == 0
