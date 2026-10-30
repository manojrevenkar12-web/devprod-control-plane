"""`devprod export-web`: the dashboard's data, written to web/public/data. Part of the evaluation layer:
it may join the observed record with the simulation's truth (which releases were really bad, which
tests are really flaky), and every truth-derived field is named `truth_*` so the dashboard labels it."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import settings
from .store import Store

DAY = 24 * 60


def _truth(policy: str, name: str) -> Any:
    return json.loads((settings.truth(policy) / f"{name}.json").read_text())


def daily(policy: str) -> list[dict[str, Any]]:
    s = Store(settings.observed(policy))
    runs = s.rows("select started_ts, finished_ts, conclusion, trigger, sha from runs")
    fleet = {r["day"]: r for r in s.rows("select day, runner_minutes, peak_runners from fleet_usage")}
    by: dict[int, dict[str, Any]] = defaultdict(lambda: {"runs": 0, "failed": 0, "wall": []})
    for r in runs:
        d = by[int(r["started_ts"] // DAY)]
        d["runs"] += 1
        d["failed"] += r["conclusion"] == "failure"
        if r["trigger"] == "push":
            d["wall"].append(r["finished_ts"] - r["started_ts"])
    out = []
    for day in sorted(set(by) | set(fleet)):
        d = by[day]
        wall = sorted(d["wall"])
        out.append(
            {
                "day": day,
                "weekday": day % 7,
                "runs": d["runs"],
                "failed": d["failed"],
                "wall_p50": round(wall[len(wall) // 2], 1) if wall else None,
                "runner_minutes": fleet.get(day, {}).get("runner_minutes", 0),
                "peak_runners": fleet.get(day, {}).get("peak_runners", 0),
            }
        )
    return out


def flaky() -> dict[str, Any]:
    s = Store(settings.observed("control-plane"))
    truth = _truth("control-plane", "tests")
    q = s.rows(
        "select test_id, owner, since_ts, until_ts, reason, status, clean_runs from quarantine order by since_ts"
    )
    from .flaky.detector import replay

    reg = replay(s)
    suspects = [
        {
            **h,
            "truth_flake_rate": truth.get(h["test_id"], {}).get("flake_rate", 0.0),
            "quarantined": any(r["test_id"] == h["test_id"] for r in q),
        }
        for h in reg.summary()[:40]
    ]
    return {
        "quarantine": [{**r, "truth_flake_rate": truth.get(r["test_id"], {}).get("flake_rate", 0.0)} for r in q],
        "suspects": suspects,
        "truly_flaky": len(truth),
    }


def triage(limit: int = 80) -> list[dict[str, Any]]:
    s = Store(settings.observed("control-plane"))
    truth = {f["run_id"]: f for f in _truth("control-plane", "failures")}
    rows = s.rows(
        "select c.run_id, c.pr, c.ts, c.classification, c.body, r.attempt, r.trigger from comments c "
        "join runs r using(run_id) order by c.ts desc limit ?",
        (limit,),
    )
    out = []
    for r in rows:
        logs = s.rows(
            "select j.module, j.step_failed, l.text from jobs j left join logs l using(job_id) "
            "where j.run_id=? and j.conclusion='failure' limit 1",
            (r["run_id"],),
        )
        tail = (logs[0]["text"] or "")[-900:] if logs else ""
        t = truth.get(r["run_id"], {})
        out.append(
            {
                **r,
                "module": logs[0]["module"] if logs else None,
                "log_tail": tail,
                "truth_kind": t.get("kind"),
                "final_verdict": t.get("agent"),
            }
        )
    return out


def rollouts(policy: str, variant: str | None = None) -> dict[str, Any]:
    d = _truth(policy, "deploys" if variant is None else f"deploys-{variant}")
    s = Store(settings.observed(policy))
    accounts = s.rows("select account_id, tier, traffic, window from accounts order by traffic, account_id")
    order = [a["account_id"] for a in accounts if not a["account_id"].startswith("internal-")]
    rel = {r["release"]: r for r in d["releases"]}
    out = []
    for ro in d["rollouts"]:
        deployed = ro["deployed"]
        state = ro["state"]
        strip = "".join(("r" if state == "rolled_back" else "d") if a in deployed else "." for a in order)
        stopped = next(
            (e.get("wave") for e in reversed(ro["events"]) if "gate" in e and e["gate"].get("passed") is False), None
        )
        out.append(
            {
                "release": ro["release"],
                "state": state,
                "truth_bad": bool(rel[ro["release"]]["bad_prs"]),
                "internal": sorted(a for a in deployed if a.startswith("internal-")),
                "customers_reached": sum(c != "." for c in strip),
                "strip": strip,
                "stopped_at": stopped,
                "started": min(deployed.values()) if deployed else None,
            }
        )
    return {
        "policy": policy,
        "variant": variant,
        "accounts": [{k: a[k] for k in ("tier", "traffic", "window")} for a in accounts if a["account_id"] in order],
        "rollouts": out,
    }


def heldout() -> list[dict[str, Any]]:
    base = settings.REPORTS / "heldout"
    if not base.exists():
        return []
    out = []
    for p in sorted(base.iterdir()):
        first = p / "first_run.json"
        if first.exists():
            r = json.loads(first.read_text())
            out.append(
                {
                    k: r[k]
                    for k in (
                        "set",
                        "status",
                        "cases",
                        "kind_accuracy",
                        "next_step_accuracy",
                        "by_kind",
                        "confusion",
                        "authors_sent_chasing_noise",
                    )
                }
                | {"misses": [x for x in r["results"] if not x["kind_ok"]]}
            )
    return out


def run(out: Path | None = None) -> list[str]:
    out = out or settings.ROOT / "web" / "public" / "data"
    out.mkdir(parents=True, exist_ok=True)
    results = json.loads((settings.REPORTS / "results.json").read_text())
    files = {
        "summary.json": {
            "start": str(settings.load("org")["start"]),
            **{k: results[k] for k in ("worlds", "ablations", "gate")},
        },
        "daily.json": {p: daily(p) for p in settings.POLICIES},
        "flaky.json": flaky(),
        "triage.json": triage(),
        "rollouts.json": {
            "baseline": rollouts("baseline"),
            "control-plane": rollouts("control-plane"),
            **{v: rollouts("control-plane", v) for v in results.get("ablations", {})},
        },
        "heldout.json": heldout(),
    }
    for name, data in files.items():
        (out / name).write_text(json.dumps(data, separators=(",", ":"), default=float))
    return sorted(files)
