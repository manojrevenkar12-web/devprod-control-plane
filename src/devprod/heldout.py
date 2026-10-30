"""Held-out CI failures: cases written blind from heldout/WRITING_FAILURES.md by someone who has not
seen the agent. `devprod heldout <set>` builds a small CI record for each case (the pull request, the
failed jobs and their logs, the failing tests' history, quarantine, recent install failures), asks the
triage agent, and scores its kind and next step against the writer's.

The first run of a set is written once to reports/heldout/<set>/first_run.json and never rewritten;
later runs go to latest.json. Never change the agent to pass a set: if its failures are studied,
write `regression` to heldout/<set>/STATUS and have someone who has not seen the code write a new set."""

from __future__ import annotations

import json
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from . import settings
from .agent.tools import Tools
from .agent.triage import KINDS, StandIn
from .ci.graph import Graph
from .flaky.detector import FlakeRegister
from .store import Store

NOW = 10_000.0


def load(name: str) -> list[dict[str, Any]]:
    return yaml.safe_load((settings.HELDOUT / name / "cases.yaml").read_text())


def status(name: str) -> str:
    p = settings.HELDOUT / name / "STATUS"
    return p.read_text().strip() if p.exists() else "heldout"


def record(case: dict[str, Any], modules: dict[str, dict[str, Any]], path: Path) -> tuple[Store, FlakeRegister]:
    s = Store(path, fresh=True)
    for name, m in modules.items():
        s.insert("modules", {"name": name, "language": m["language"], "deps": m["deps"], "owner": m["owner"],
                             "path": m["path"]})
    pr = case["pr"]
    s.insert("prs", {"pr": 1, "author": "writer", "team": "t", "opened_ts": NOW - 60, "merged_ts": None,
                     "modules": pr.get("modules", []), "files": pr.get("files", []), "title": case["id"],
                     "head_sha": "head"})
    s.insert("runs", {"run_id": 1, "pr": 1, "sha": "head", "attempt": 1, "trigger": "push",
                      "started_ts": NOW - 15, "finished_ts": NOW, "conclusion": "failure"})
    own_install = False
    for j in case["failed_jobs"]:
        job = s.insert("jobs", {"run_id": 1, "module": j["module"], "shard": 0, "runner": "r", "queued_ts": NOW - 15,
                                "started_ts": NOW - 15, "finished_ts": NOW, "conclusion": "failure",
                                "step_failed": j.get("step", "test"), "minutes": 15, "cache_hit": 0})
        s.insert("logs", {"job_id": job, "text": j["log"]})
        own_install |= j.get("step") == "install"
    others = max(0, int(case.get("install_failures_last_2h", 0)) - (1 if own_install else 0))
    for k in range(others):  # other pull requests failing at install in the last two hours
        s.insert("runs", {"run_id": 100 + k, "pr": 100 + k, "sha": f"o{k}", "attempt": 1, "trigger": "push",
                          "started_ts": NOW - 90, "finished_ts": NOW - 30 - k, "conclusion": "failure"})
        s.insert("jobs", {"run_id": 100 + k, "module": "core", "shard": 0, "runner": "r", "queued_ts": NOW - 90,
                          "started_ts": NOW - 90, "finished_ts": NOW - 30 - k, "conclusion": "failure",
                          "step_failed": "install", "minutes": 2, "cache_hit": 0})
    for t in case.get("quarantined") or []:
        s.insert("quarantine", {"test_id": t, "since_ts": 0, "until_ts": NOW * 2, "owner": "x", "reason": "case",
                                "status": "quarantined", "clean_runs": 0})
    s.commit()
    reg = FlakeRegister(s, {n: m["owner"] for n, m in modules.items()})
    for test, h in (case.get("history") or {}).items():
        key = f"{test.split('::')[0]}#0"
        runs, fails, flips = int(h.get("runs", 0)), int(h.get("fails", 0)), int(h.get("flips", 0))
        flips = min(flips, fails)
        n = 0
        for k in range(flips):  # failed, then passed on the same commit
            reg.observe_job(f"f{k}", key, [test], [test])
            reg.observe_job(f"f{k}", key, [test], [])
            n += 2
        for k in range(fails - flips):  # failed and stayed failed
            reg.observe_job(f"x{k}", key, [test], [test])
            n += 1
        for k in range(max(0, runs - n)):
            reg.observe_job(f"p{k}", key, [test], [])
    return s, reg


def judge(case: dict[str, Any], modules: dict[str, dict[str, Any]]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        s, reg = record(case, modules, Path(tmp) / "case.sqlite")
        confirmed = {None: None, "failed": True, "passed": False}[case.get("confirming_rerun")]
        d = StandIn(Tools(s, Graph(s.modules()), reg)).diagnose(1, 1, NOW, confirmed=confirmed)
        s.close()
    want = case["expect"]
    return {"id": case["id"], "expected": want["kind"], "got": d.kind, "kind_ok": d.kind == want["kind"],
            "expected_next": want.get("next_step"), "got_next": d.next_step,
            "next_ok": d.next_step == want.get("next_step"), "confidence": d.confidence,
            "evidence": d.evidence[:3], "why": case.get("why", "")}


def run(name: str, modules: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    if modules is None:
        modules = Store(settings.observed("control-plane")).modules()
    cases = load(name)
    results = [judge(c, modules) for c in cases]
    confusion = {k: dict(Counter(r["got"] for r in results if r["expected"] == k)) for k in KINDS}
    out = {
        "set": name,
        "status": status(name),
        "cases": len(results),
        "kind_accuracy": round(sum(r["kind_ok"] for r in results) / len(results), 3),
        "next_step_accuracy": round(sum(r["next_ok"] for r in results) / len(results), 3),
        "by_kind": {k: {"cases": sum(1 for r in results if r["expected"] == k),
                        "right": sum(1 for r in results if r["expected"] == k and r["kind_ok"])} for k in KINDS},
        "confusion": confusion,
        "authors_sent_chasing_noise": sum(1 for r in results if r["got"] == "regression" and r["expected"] != "regression"
                                          and r["got_next"] == "tell_author"),
        "results": results,
    }
    folder = settings.REPORTS / "heldout" / name
    folder.mkdir(parents=True, exist_ok=True)
    first = folder / "first_run.json"
    if not first.exists():
        first.write_text(json.dumps(out, indent=2))
        out["first_run"] = True
    (folder / "latest.json").write_text(json.dumps(out, indent=2))
    return out


def summary(name: str) -> str:
    p = settings.REPORTS / "heldout" / name / "first_run.json"
    if not p.exists():
        return f"`{name}`: not run yet."
    r = json.loads(p.read_text())
    lines = [f"`{name}` ({r['status']}), first run: {r['cases']} cases written blind; kind right "
             f"{r['kind_accuracy']:.0%}, next step right {r['next_step_accuracy']:.0%}.", "",
             "| Expected | Cases | Right |", "|---|---|---|"]
    lines += [f"| {k} | {v['cases']} | {v['right']} |" for k, v in r["by_kind"].items()]
    return "\n".join(lines)
