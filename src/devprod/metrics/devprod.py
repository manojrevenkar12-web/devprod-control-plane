"""Developer-productivity and delivery metrics, computed only from what GitHub, the CI, the cloud bill
and the deploy service record (the observed store). These are the numbers a platform team would put
on its own dashboard; `devprod.evaluation` checks them against the truth.

Times are minutes since the start of the period. Working days are Monday to Friday."""

from __future__ import annotations

import json
from collections import defaultdict
from itertools import pairwise
from typing import Any

import numpy as np

from ..store import Store

DAY = 24 * 60


def _q(xs: list[float], q: float) -> float | None:
    return round(float(np.quantile(xs, q)), 1) if xs else None


def working_days(t0: float, t1: float) -> int:
    return sum(1 for d in range(int(t0 // DAY), int(t1 // DAY) + 1) if d % 7 < 5)


def ci(store: Store) -> dict[str, Any]:
    prs = store.rows("select pr, author, opened_ts, merged_ts from prs")
    runs = store.rows("select run_id, pr, sha, attempt, trigger, started_ts, finished_ts, conclusion from runs")
    jobs = store.rows(
        "select run_id, started_ts, queued_ts, finished_ts, minutes, conclusion, step_failed, cache_hit from jobs"
    )
    merged = [p for p in prs if p["merged_ts"] is not None]
    lead = [(p["merged_ts"] - p["opened_ts"]) / 60 for p in merged]
    first_runs = [r for r in runs if r["trigger"] == "push"]
    wall = [r["finished_ts"] - r["started_ts"] for r in first_runs]
    # a run that failed and then passed on the same commit without a new push is a flake as people see it
    by_sha: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in sorted(runs, key=lambda r: r["started_ts"]):
        by_sha[r["sha"]].append(r)
    failed_then_passed = sum(
        1
        for rs in by_sha.values()
        for a, b in pairwise(rs)
        if a["conclusion"] == "failure" and b["conclusion"] == "success"
    )
    failed_push = sum(1 for r in first_runs if r["conclusion"] == "failure")
    # time from a push to a green signal on that commit (the number engineers feel)
    to_green = []
    for rs in by_sha.values():
        ok = next((r for r in rs if r["conclusion"] == "success"), None)
        if ok is not None:
            to_green.append(ok["finished_ts"] - rs[0]["started_ts"])
    queue = [j["started_ts"] - j["queued_ts"] for j in jobs]
    span = (min(p["opened_ts"] for p in prs), max(p["merged_ts"] or p["opened_ts"] for p in prs)) if prs else (0, 0)
    engineers = len({p["author"] for p in prs})
    days = working_days(*span)
    fleet = store.rows("select sum(runner_minutes) m, max(peak_runners) peak from fleet_usage")[0]
    steps = defaultdict(int)
    for j in jobs:
        if j["conclusion"] == "failure":
            steps[j["step_failed"] or "unknown"] += 1
    return {
        "prs": len(prs),
        "merged": len(merged),
        "pr_lead_time_hours": {"p50": _q(lead, 0.5), "p90": _q(lead, 0.9)},
        "ci_wall_minutes": {"p50": _q(wall, 0.5), "p95": _q(wall, 0.95)},
        "push_to_green_minutes": {"p50": _q(to_green, 0.5), "p95": _q(to_green, 0.95)},
        "queue_minutes": {"mean": round(float(np.mean(queue)), 2) if queue else None, "p95": _q(queue, 0.95)},
        "runs": len(runs),
        "failed_push_runs": failed_push,
        "failed_push_share": round(failed_push / len(first_runs), 3) if first_runs else None,
        "failed_then_passed_same_commit": failed_then_passed,
        "jobs": len(jobs),
        "job_minutes": round(sum(j["minutes"] for j in jobs), 0),
        "cache_hit_share": round(float(np.mean([j["cache_hit"] for j in jobs])), 3) if jobs else None,
        "failed_jobs_by_step": dict(sorted(steps.items())),
        "runner_minutes": round(fleet["m"] or 0, 0),
        "peak_runners": fleet["peak"],
        "engineers": engineers,
        "working_days": days,
        "prs_per_engineer_day": round(len(merged) / (engineers * days), 3) if engineers and days else None,
        "quarantined_tests": store.rows("select count(*) n from quarantine")[0]["n"],
        "triage_comments": store.rows("select count(*) n from comments where author='triage-agent'")[0]["n"],
        "engineer_minutes_on_failures": round(
            store.rows("select coalesce(sum(minutes),0) m from engineer_time")[0]["m"], 0
        ),
    }


def delivery(store: Store) -> dict[str, Any]:
    """DORA's four keys from the deploy service's own records."""
    rel = {r["release_id"]: r for r in store.rows("select release_id, ts, shas from releases")}
    dep = store.rows("select release_id, account_id, wave, started_ts, finished_ts, status from deployments")
    commits = {r["sha"]: r["merged_ts"] for r in store.rows("select sha, merged_ts from commits")}
    by_rel: dict[int, list[dict[str, Any]]] = defaultdict(list)
    rollbacks: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for d in dep:
        (rollbacks if d["status"] == "rollback" else by_rel)[d["release_id"]].append(d)
    customer = {k: [d for d in v if not d["account_id"].startswith("internal-")] for k, v in by_rel.items()}
    reached = {k: v for k, v in customer.items() if v}
    completed = {k for k, v in by_rel.items() if v and v[0]["status"] == "completed"}
    failed = {k for k, v in reached.items() if v[0]["status"] == "rolled_back"}
    # lead time for changes: merge -> 95% of customer accounts on a release that contains it
    done_at = {
        k: float(np.quantile([d["finished_ts"] for d in customer[k]], 0.95)) for k in completed if customer.get(k)
    }
    lead = []
    order = sorted(rel)
    for k in order:
        later = [done_at[j] for j in order if j >= k and j in done_at]
        if not later:
            continue
        for sha in json.loads(rel[k]["shas"] or "[]"):
            if sha in commits:
                lead.append((min(later) - commits[sha]) / 60)
    # time to restore: first customer account on a failed release -> last customer account rolled back
    restore = []
    for k in failed:
        back = [d["finished_ts"] for d in rollbacks.get(k, []) if not d["account_id"].startswith("internal-")]
        if back:
            restore.append(max(back) - min(d["finished_ts"] for d in reached[k]))
    span = (min(r["ts"] for r in rel.values()), max(r["ts"] for r in rel.values())) if rel else (0, 0)
    days = working_days(*span)
    return {
        "releases": len(rel),
        "rollouts_reaching_customers": len(reached),
        "completed_rollouts": len(completed),
        "deploys_per_working_day": round(len(completed) / days, 2) if days else None,
        "change_failure_rate": round(len(failed) / len(reached), 3) if reached else None,
        "lead_time_for_changes_hours": {"p50": _q(lead, 0.5), "p90": _q(lead, 0.9)},
        "time_to_restore_minutes": {"p50": _q(restore, 0.5), "max": round(max(restore), 1) if restore else None},
        "rollbacks": sum(len(v) for v in rollbacks.values()),
        "customer_accounts_touched_by_failed_rollouts": len({d["account_id"] for k in failed for d in reached[k]}),
        "failed_rollout_customer_accounts_median": _q([len(reached[k]) for k in failed], 0.5),
    }


def compute(store: Store) -> dict[str, Any]:
    return {"ci": ci(store), "delivery": delivery(store)}
