"""Ten weeks of an engineering organisation, run under a policy: pull requests are opened, CI runs on
a runner fleet, tests pass, fail or flake, people (or the triage agent) react, reviews happen, changes
merge and are released. Both policies see the same pull requests, the same flaky draws and the same
outages (common random numbers), so differences come from the policy alone.

The control plane's own code is in the loop: module selection, sharding, the runner autoscaler, the
flake register and the triage agent. What the simulator adds is what really happens (truth): which
tests are flaky, which changes break what, how long people take, and which releases are bad."""

from __future__ import annotations

import hashlib
import heapq
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np

from .. import settings
from ..agent.tools import Tools
from ..agent.triage import StandIn, costs
from ..ci.fleet import Fleet
from ..ci.graph import Graph
from ..ci.sharding import shard
from ..flaky.detector import FlakeRegister
from ..store import Store
from . import logs as L
from .repo import Module, Test, build, rng

DAY = 24 * 60


def u(*key: Any) -> float:
    """A uniform draw fixed by its key: the same test on the same commit and attempt flakes or not in
    every policy."""
    h = hashlib.sha256("|".join(map(str, (settings.load("org")["seed"], *key))).encode()).digest()
    return int.from_bytes(h[:8], "little") / 2**64


def working(t: float) -> bool:
    d, m = divmod(t, DAY)
    lo, hi = settings.load("org")["people"]["workday"]
    return int(d) % 7 < 5 and lo * 60 <= m < hi * 60


def next_work(t: float) -> float:
    lo, _ = settings.load("org")["people"]["workday"]
    while not working(t):
        d = math.floor(t / DAY)
        m = t - d * DAY
        if int(d) % 7 < 5 and m < lo * 60:
            t = d * DAY + lo * 60
        else:
            t = (d + 1) * DAY + lo * 60
    return t


def add_work(t: float, minutes: float) -> float:
    """t plus `minutes` of working time."""
    t = next_work(t)
    _, hi = settings.load("org")["people"]["workday"]
    while minutes > 0:
        end_of_day = math.floor(t / DAY) * DAY + hi * 60
        step = min(minutes, end_of_day - t)
        t += step
        minutes -= step
        if minutes > 0:
            t = next_work(t + 1e-6)
    return t


@dataclass
class PR:
    pr: int
    author: str
    team: str
    opened: float
    modules: list[str]
    files: list[str]
    title: str
    regression: bool
    covered: bool
    failing: list[str]
    slow: bool
    review_minutes: float
    fix_minutes: float
    shas: list[str] = field(default_factory=list)
    merged: float | None = None
    escaped: bool = False
    hidden_by_quarantine: bool = False
    caught: bool = False


def prs(mods: dict[str, Module], graph: Graph) -> list[PR]:
    cfg = settings.load("org")
    pc, cc = cfg["people"], cfg["changes"]
    r = rng("prs")
    team_mods: dict[str, list[str]] = defaultdict(list)
    for m in mods.values():
        team_mods[m.owner].append(m.name)
    engineers = []
    for team, n in pc["teams"].items():
        engineers += [(f"{team}-{k + 1:02d}", team) for k in range(n)]
    out: list[PR] = []
    n = 0
    for day in range(cfg["weeks"] * 7):
        if day % 7 >= 5:
            continue
        for eng, team in engineers:
            if not team_mods[team] or r.random() > pc["prs_per_engineer_day"]:
                continue
            n += 1
            opened = (
                day * DAY + pc["workday"][0] * 60 + float(r.uniform(0, (pc["workday"][1] - pc["workday"][0] - 1) * 60))
            )
            k = int(r.integers(cc["modules_per_pr"][0], cc["modules_per_pr"][1] + 1))
            touched = sorted(
                {str(x) for x in r.choice(team_mods[team], size=min(k, len(team_mods[team])), replace=False)}
            )
            files = []
            for m in touched:
                t = mods[m].tests[int(r.integers(len(mods[m].tests)))]
                src = t.file.replace("/tests/test_", "/src/").replace(".test.tsx", ".tsx")
                files += [src, t.file] if r.random() < 0.6 else [src]
            regression = bool(r.random() < cc["regression_share"])
            covered = regression and bool(r.random() < cc["covered_share"])
            failing: list[str] = []
            if covered:
                target = touched[int(r.integers(len(touched)))]
                deps = sorted(graph.affected([target]) - {target})
                if deps and r.random() < cc["breaks_dependents_share"]:
                    target = deps[int(r.integers(len(deps)))]
                pool = [t.test_id for t in mods[target].tests]
                failing = [
                    str(x)
                    for x in r.choice(
                        pool, size=min(len(pool), int(r.integers(*cc["regression_tests"]))), replace=False
                    )
                ]
            out.append(
                PR(
                    n,
                    eng,
                    team,
                    opened,
                    touched,
                    files,
                    f"{touched[0]}: change #{n}",
                    regression,
                    covered,
                    failing,
                    bool(r.random() < cc["slow_regression_share"]),
                    float(np.exp(r.normal(np.log(pc["review_hours"]["median"] * 60), pc["review_hours"]["sd"]))),
                    float(np.exp(r.normal(np.log(pc["fix_minutes"]["median"]), pc["fix_minutes"]["sd"]))),
                )
            )
    return out


def outages() -> list[tuple[float, float, str]]:
    cfg = settings.load("org")
    r = rng("outages")
    out = []
    for _ in range(cfg["ci"]["dependency_outages"]):
        day = int(r.integers(0, cfg["weeks"] * 7))
        day += 2 if day % 7 >= 5 else 0
        start = day * DAY + float(r.uniform(9 * 60, 16 * 60))
        out.append(
            (start, start + float(r.uniform(*cfg["ci"]["outage_hours"])) * 60, str(r.choice(["python", "typescript"])))
        )
    return out


class Policy:
    def __init__(self, name: str) -> None:
        self.name = name
        self.cfg = settings.load("ci")[name]


@dataclass(order=True)
class Event:
    t: float
    seq: int
    kind: str = field(compare=False)
    pr: int = field(compare=False)
    data: dict[str, Any] = field(compare=False, default_factory=dict)


class World:
    def __init__(self, policy: str) -> None:
        self.cfg = settings.load("org")
        self.policy = Policy(policy)
        self.mods = build()
        self.tests: dict[str, Test] = {t.test_id: t for m in self.mods.values() for t in m.tests}
        self.store = Store(settings.observed(policy), fresh=True)
        for m in self.mods.values():
            self.store.insert(
                "modules", {"name": m.name, "language": m.language, "deps": m.deps, "owner": m.owner, "path": m.path}
            )
            self.store.insert_many(
                "tests", [{"test_id": t.test_id, "module": m.name, "seconds": t.seconds} for t in m.tests]
            )
        self.graph = Graph(self.store.modules())
        self.fleet = Fleet(
            "fixed" if self.policy.cfg["runners"] == "fixed" else "autoscale", settings.load("ci")["fleet"]
        )
        self.flakes = FlakeRegister(self.store, {m.name: m.owner for m in self.mods.values()})
        self.tools = Tools(self.store, self.graph, self.flakes)
        self.agent = StandIn(self.tools)
        self.prs = {p.pr: p for p in prs(self.mods, self.graph)}
        self.outages = outages()
        self.events: list[Event] = []
        self.seq = 0
        self.run_id = 0
        self.truth_failures: list[dict[str, Any]] = []
        self.failure_by_run: dict[int, dict[str, Any]] = {}
        self.meta: dict[int, dict[str, Any]] = {}
        self.reviewed: set[int] = set()
        self.attempt_of: dict[int, int] = {}
        self.human: list[dict[str, Any]] = []
        self.agent_usd = 0.0
        self.durations = {tid: t.seconds for tid, t in self.tests.items()}  # what JUnit reports record
        self.shards: dict[str, list[list[str]]] = {}
        self.flaky_in: dict[tuple[str, int], list[str]] = {}
        for m in self.mods.values():
            durations = {t.test_id: self.durations[t.test_id] for t in m.tests}
            self.shards[m.name] = shard(
                durations, self.policy.cfg["shard_target_minutes"], self.policy.cfg["max_shards"]
            )
            for k, tests in enumerate(self.shards[m.name]):
                self.flaky_in[(m.name, k)] = [t for t in tests if self.tests[t].flake_rate > 0]
        self.shard_minutes = {
            (m, k): sum(self.tests[t].seconds for t in ts) / 60
            for m, shards in self.shards.items()
            for k, ts in enumerate(shards)
        }

    def push(self, t: float, kind: str, pr: int, **data: Any) -> None:
        self.seq += 1
        heapq.heappush(self.events, Event(t, self.seq, kind, pr, data))

    # ---------------------------------------------------------------- CI
    def select(self, p: PR) -> list[str]:
        sel = self.policy.cfg["selection"]
        if sel == "all":
            return sorted(self.mods)
        if sel == "touched":
            return sorted(p.modules)
        return sorted(self.graph.affected(p.modules))

    def run_ci(self, p: PR, sha: str, t: float, only: list[tuple[str, int]] | None, attempt: int, trigger: str) -> None:
        self.run_id += 1
        run_id = self.run_id
        quarantined = self.store.quarantined() if self.policy.cfg["quarantine"] else set()
        jobs_spec: list[tuple[str, int, list[str]]] = []
        for m in self.select(p):
            for k, tests in enumerate(self.shards[m]):
                if only is None or (m, k) in only:
                    jobs_spec.append((m, k, tests))
        end_all = t
        failed_jobs: list[tuple[str, int]] = []
        causes: set[str] = set()
        results_rows = []
        for m, k, tests in jobs_spec:
            mod = self.mods[m]
            hit = u("cache", sha, m, attempt) < self.policy.cfg["cache_hit"]
            sc = self.cfg["repo"]["setup_minutes"]
            setup = sc["cache_hit"] if hit else (sc["cache_miss"] if mod.language == "python" else sc["ts_cache_miss"])
            minutes = setup + self.shard_minutes[(m, k)] + 0.4
            runner, start, end = self.fleet.run(t, minutes)
            conclusion, step, log = "success", None, None
            outage = next((o for o in self.outages if o[0] <= start < o[1] and o[2] == mod.language), None)
            r = rng("log", run_id, m, k)
            if outage and not hit and u("dep", sha, m, k, attempt) < 0.9:
                conclusion, step, log = "failure", "install", L.dependency(mod, k, runner, r)
                end = start + setup * 0.6
                causes.add("dependency")
            elif u("infra", sha, m, k, attempt, trigger) < self.cfg["ci"]["infra_failure_per_job"]:
                kinds = self.cfg["ci"]["infra_kinds"]
                kind = str(r.choice(list(kinds), p=np.array(list(kinds.values())) / sum(kinds.values())))
                conclusion, step, log = "failure", "test", L.infra(mod, k, runner, kind)
                end = start + minutes * float(u("infra-at", sha, m, k, attempt))
                causes.add("infra")
            else:
                fails: list[tuple[Test, str]] = []
                gating_fail = False
                candidates = [tid for tid in self.flaky_in[(m, k)] if tid in tests]
                if sha == p.shas[0] and p.covered:
                    candidates += [tid for tid in p.failing if tid in tests and tid not in candidates]
                for tid in candidates:
                    test = self.tests[tid]
                    regress = tid in p.failing and sha == p.shas[0] and p.covered
                    flake = test.flake_rate > 0 and u("flake", tid, sha, attempt) < test.flake_rate
                    if regress or flake:
                        kind = ("timeout" if p.slow else "regression") if regress else (test.flake_kind or "timeout")
                        fails.append((test, kind))
                        q = tid in quarantined
                        results_rows.append(
                            {
                                "job_id": 0,
                                "run_id": run_id,
                                "sha": sha,
                                "test_id": tid,
                                "outcome": "quarantined_failed" if q else "failed",
                                "seconds": test.seconds,
                                "ts": end,
                            }
                        )
                        if not q:
                            gating_fail = True
                            causes.add("regression" if regress else "flaky")
                            if regress:
                                p.caught = True
                        elif regress:
                            p.hidden_by_quarantine = True
                if gating_fail:
                    conclusion, step = "failure", "test"
                    log = L.tests_failed(
                        mod,
                        k,
                        runner,
                        [f for f in fails if f[0].test_id not in quarantined],
                        len(tests) - len(fails),
                        r,
                    )
                self.flakes.observe_job(sha, f"{m}#{k}", tests, [x.test_id for x, _ in fails])
            job_id = self.store.insert(
                "jobs",
                {
                    "run_id": run_id,
                    "module": m,
                    "shard": k,
                    "runner": runner,
                    "queued_ts": t,
                    "started_ts": start,
                    "finished_ts": end,
                    "conclusion": conclusion,
                    "step_failed": step,
                    "minutes": end - start,
                    "cache_hit": int(hit),
                },
            )
            for row in results_rows:
                if row["job_id"] == 0:
                    row["job_id"] = job_id
            if log:
                self.store.insert("logs", {"job_id": job_id, "text": log})
            if conclusion == "failure":
                failed_jobs.append((m, k))
            end_all = max(end_all, end)
        self.store.insert_many("test_results", results_rows)
        conclusion = "failure" if failed_jobs else "success"
        self.store.insert(
            "runs",
            {
                "run_id": run_id,
                "pr": p.pr,
                "sha": sha,
                "attempt": attempt,
                "trigger": trigger,
                "started_ts": t,
                "finished_ts": end_all,
                "conclusion": conclusion,
            },
        )
        if failed_jobs:
            kind = next(k for k in ("dependency", "infra", "regression", "flaky") if k in causes)
            self.attempt_of[run_id] = attempt
            rec = {"run_id": run_id, "pr": p.pr, "sha": sha, "kind": kind, "causes": sorted(causes), "ts": end_all}
            self.truth_failures.append(rec)
            self.failure_by_run[run_id] = rec
        self.push(
            end_all, "ci_done", p.pr, run_id=run_id, sha=sha, attempt=attempt, failed=failed_jobs, causes=sorted(causes)
        )

    # ---------------------------------------------------------------- reactions
    def human_time(self, p: PR, run_id: int, t: float, minutes: float, kind: str) -> float:
        self.human.append({"pr": p.pr, "run_id": run_id, "ts": t, "minutes": minutes, "kind": kind})
        self.store.insert("engineer_time", {"pr": p.pr, "run_id": run_id, "ts": t, "minutes": minutes, "kind": kind})
        return add_work(t, minutes)

    def on_failure(self, p: PR, e: Event) -> None:
        run_id, sha, attempt, failed, causes = (e.data[k] for k in ("run_id", "sha", "attempt", "failed", "causes"))
        truth = next(k for k in ("dependency", "infra", "regression", "flaky") if k in causes)
        pc = self.cfg["people"]
        base = pc["investigate_minutes"][truth] * float(np.exp(rng("inv", run_id).normal(0, 0.35)))
        if self.policy.cfg["triage"] == "person":
            t = self.human_time(p, run_id, next_work(e.t + 10), base, truth)
            self.act_as_person(p, truth, sha, attempt, t)
            return
        confirm = e.data.get("confirming")
        d = self.agent.diagnose(p.pr, run_id, e.t, confirmed=None if confirm is None else True)
        self.agent_usd += costs(d.calls)
        self.tools.comment(p.pr, e.t + 2, d.body(), d.kind, run_id)
        correct = d.kind == truth
        self.failure_by_run[run_id].update({"agent": d.kind, "agent_correct": correct, "next_step": d.next_step})
        origin = e.data.get("origin")
        if confirm and origin in self.failure_by_run:
            self.failure_by_run[run_id]["confirming_of"] = origin
        if confirm and origin in self.failure_by_run and d.kind == "regression":  # failed again: final verdict
            o = self.failure_by_run[origin]
            o.update({"agent": d.kind, "agent_correct": d.kind == o["kind"], "confirmed_by_rerun": False})
        retries = e.data.get("auto_retries", 0)
        if d.next_step in ("rerun_failed_jobs", "rerun_to_confirm") and retries < 1:
            self.push(
                e.t + 2,
                "rerun",
                p.pr,
                sha=sha,
                only=failed,
                attempt=attempt + 1,
                auto_retries=retries + 1,
                confirming=d.next_step == "rerun_to_confirm",
                origin=run_id,
            )
            return
        if d.next_step == "wait_and_rerun":
            end = max((o[1] for o in self.outages if o[0] <= e.t + 1 < o[1] + 30), default=e.t + 30)
            self.push(
                max(end + 5, e.t + 30), "rerun", p.pr, sha=sha, only=failed, attempt=attempt + 1, auto_retries=retries
            )
            return
        factor = pc["investigate_with_correct_hint"] if correct else pc["investigate_with_wrong_hint"]
        t = self.human_time(p, run_id, next_work(e.t + 10), base * factor, truth)
        self.act_as_person(p, truth, sha, attempt, t)

    def act_as_person(self, p: PR, truth: str, sha: str, attempt: int, t: float) -> None:
        """A person who has understood the failure: rerun, wait out an outage, or fix the change."""
        if truth == "regression":
            t = add_work(t, p.fix_minutes)
            new = hashlib.sha1(f"{p.pr}-{len(p.shas)}".encode()).hexdigest()[:10]
            p.shas.append(new)
            self.push(t, "push", p.pr, sha=new)
        elif truth == "dependency":
            end = max((o[1] for o in self.outages if o[0] <= t < o[1]), default=t)
            self.push(next_work(end + 10), "rerun", p.pr, sha=sha, only=None, attempt=attempt + 1)
        else:
            self.push(t, "rerun", p.pr, sha=sha, only=None, attempt=attempt + 1)

    # ---------------------------------------------------------------- the loop
    def run(self) -> dict[str, Any]:
        for p in self.prs.values():
            sha = hashlib.sha1(f"{p.pr}-0".encode()).hexdigest()[:10]
            p.shas.append(sha)
            self.store.insert(
                "prs",
                {
                    "pr": p.pr,
                    "author": p.author,
                    "team": p.team,
                    "opened_ts": p.opened,
                    "merged_ts": None,
                    "modules": p.modules,
                    "files": p.files,
                    "title": p.title,
                    "head_sha": sha,
                },
            )
            self.push(p.opened, "push", p.pr, sha=sha)
        last_review = 0.0
        while self.events:
            e = heapq.heappop(self.events)
            p = self.prs[e.pr]
            if e.kind == "push":
                self.run_ci(p, e.data["sha"], e.t, None, 1, "push")
            elif e.kind == "rerun":
                self.run_ci(
                    p,
                    e.data["sha"],
                    e.t,
                    e.data.get("only"),
                    e.data["attempt"],
                    "rerun_failed" if e.data.get("only") else "rerun",
                )
                self.events_meta(e)
            elif e.kind == "ci_done":
                pending = self.meta.pop(e.data["run_id"], {})
                e.data.update(pending)
                if e.data["failed"]:
                    self.on_failure(p, e)
                elif e.data["sha"] == p.shas[-1] and p.merged is None:
                    origin = e.data.get("origin")
                    if e.data.get("confirming") and origin in self.failure_by_run:
                        self.failure_by_run[origin].update({"agent": "flaky", "confirmed_by_rerun": True})
                        self.failure_by_run[origin]["agent_correct"] = self.failure_by_run[origin]["kind"] == "flaky"
                    approved = add_work(max(e.t, p.opened), p.review_minutes) if p.pr not in self.reviewed else e.t
                    self.reviewed.add(p.pr)
                    self.push(max(approved, e.t), "merge", p.pr)
            elif e.kind == "merge" and p.merged is None:
                p.merged = e.t
                p.escaped = p.regression and not p.caught
                self.store.update("prs", "pr", p.pr, merged_ts=e.t)
                self.store.insert("commits", {"sha": p.shas[-1], "pr": p.pr, "merged_ts": e.t})
            if self.policy.cfg["quarantine"] and e.t - last_review > 60:
                self.flakes.review(e.t)
                last_review = e.t
        self.store.commit()
        return self.finish()

    def events_meta(self, e: Event) -> None:
        self.meta[self.run_id] = {k: e.data[k] for k in ("auto_retries", "confirming", "origin") if k in e.data}

    def finish(self) -> dict[str, Any]:
        end = self.cfg["weeks"] * 7 * DAY
        truth_dir = settings.truth(self.policy.name)
        truth_dir.mkdir(parents=True, exist_ok=True)
        (truth_dir / "prs.json").write_text(
            json.dumps(
                [
                    {
                        "pr": p.pr,
                        "regression": p.regression,
                        "covered": p.covered,
                        "escaped": p.escaped,
                        "hidden_by_quarantine": p.hidden_by_quarantine,
                        "caught": p.caught,
                        "opened": p.opened,
                        "merged": p.merged,
                        "failing": p.failing,
                        "modules": p.modules,
                    }
                    for p in self.prs.values()
                ]
            )
        )
        (truth_dir / "tests.json").write_text(
            json.dumps(
                {
                    t.test_id: {"flake_rate": t.flake_rate, "kind": t.flake_kind}
                    for t in self.tests.values()
                    if t.flake_rate
                }
            )
        )
        (truth_dir / "failures.json").write_text(json.dumps(self.truth_failures))
        (truth_dir / "human.json").write_text(json.dumps(self.human))
        (truth_dir / "outages.json").write_text(json.dumps(self.outages))
        summary = {
            "policy": self.policy.name,
            "prs": len(self.prs),
            "merged": sum(1 for p in self.prs.values() if p.merged is not None),
            "runner_minutes": round(self.fleet.runner_minutes(end), 0),
            "agent_usd": round(self.agent_usd, 2),
            "start": str(date.fromisoformat(str(self.cfg["start"]))),
        }
        for row in self.fleet.usage_by_day(end):
            self.store.upsert("fleet_usage", {**row, "queue_minutes_mean": None})
        self.store.commit()
        (truth_dir / "summary.json").write_text(json.dumps(summary))
        return summary
