"""Progressive delivery of a release to every customer's AWS account.

A rollout is a resumable state machine (`advance(now)` returns when to wake next), so the same code
runs in the simulation, in a worker, or as the body of a Step Functions task, and its state is a
plain dict that can be stored between steps. Waves go from internal accounts to a small canary of
low-traffic customers, to a quarter, to everyone. Each customer may restrict deploys to a local time
window; its accounts in a wave are deployed when their window opens. After each wave bakes, a gate
compares the wave's error rate with the same accounts before the deploy; if errors more than
`max_ratio` times and a one-sided binomial test agrees at `alpha`, every account already on the
release is rolled back and the rollout stops."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from scipy.stats import binom


class Executor(Protocol):
    def deploy(self, account: str, release: int, at: float) -> float: ...  # returns when it finished
    def rollback(self, account: str, release: int, at: float) -> float: ...


class Metrics(Protocol):
    def counts(self, accounts: list[str], t0: float, t1: float) -> tuple[int, int]: ...  # (requests, errors)


@dataclass
class Account:
    account_id: str
    tier: str
    timezone: str
    window: str  # "any" or "18-07" (local hours)
    traffic: float


def in_window(a: Account, t: float, epoch: datetime) -> bool:
    if a.window == "any":
        return True
    lo, hi = (int(x) for x in a.window.split("-"))
    hour = (epoch + timedelta(minutes=t)).astimezone(ZoneInfo(a.timezone)).hour
    return hour >= lo or hour < hi if lo > hi else lo <= hour < hi


def next_window(a: Account, t: float, epoch: datetime) -> float:
    step = 15.0
    x = t
    while not in_window(a, x, epoch):
        x += step
    return x


def plan(accounts: list[Account], waves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assigns every account to a wave: internal accounts by name, then customers by share, low
    traffic and the wave's tiers first."""
    customers = sorted(
        (a for a in accounts if not a.account_id.startswith("internal-")), key=lambda a: (a.traffic, a.account_id)
    )
    out, taken = [], set()
    n = len(customers)
    for w in waves:
        if "accounts" in w:
            ids = [w["accounts"]]
        elif w.get("stratify"):  # the same share of every tier, so tier-only paths are exercised early
            ids = []
            for tier in sorted({a.tier for a in customers}):
                of_tier = [a for a in customers if a.tier == tier]
                want = math.ceil(len(of_tier) * w["share"]) - sum(1 for a in of_tier if a.account_id in taken)
                ids += [a.account_id for a in of_tier if a.account_id not in taken][: max(0, want)]
        else:
            want = math.ceil(n * w["share"]) - len(taken)
            pool = [a for a in customers if a.account_id not in taken and (not w.get("tiers") or a.tier in w["tiers"])]
            pool += [a for a in customers if a.account_id not in taken and a not in pool]
            ids = [a.account_id for a in pool[: max(0, want)]]
        taken |= {i for i in ids if not i.startswith("internal-")}
        out.append({**w, "account_ids": ids})
    return out


def gate(
    before: tuple[int, int], after: tuple[int, int], cfg: dict[str, Any], alpha: float | None = None
) -> dict[str, Any]:
    """Fails when the after-rate is significantly above max_ratio times the before-rate."""
    (n0, e0), (n1, e1) = before, after
    if n1 < cfg["min_requests"]:
        return {"passed": None, "requests": n1, "reason": f"only {n1} requests, need {cfg['min_requests']}"}
    p0 = (e0 + 1) / (n0 + 2)  # smoothed
    bound = min(0.999, cfg["max_ratio"] * p0)
    p_value = float(binom.sf(e1 - 1, n1, bound))
    failed = p_value < (cfg["alpha"] if alpha is None else alpha)
    return {
        "passed": not failed,
        "requests": n1,
        "p_value": p_value,
        "before_rate": p0,
        "after_rate": e1 / n1,
        "bound": bound,
    }


def gate_wave(
    ids: list[str],
    accounts: dict[str, Account],
    metrics: Metrics,
    t_before: float,
    t0: float,
    t1: float,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """One gate over the wave, or (per_tier) one per tier at alpha / tiers, so a fault that only one
    tier's traffic exercises is not diluted by the others. Fails if any group fails; passes once every
    group with enough traffic passed; undecided (None) while no group has enough traffic."""
    groups: dict[str, list[str]] = {}
    for a in ids:
        groups.setdefault(accounts[a].tier if cfg.get("per_tier") else "all", []).append(a)
    alpha = cfg["alpha"] / len(groups)
    out = {}
    for name, members in sorted(groups.items()):
        out[name] = gate(metrics.counts(members, t_before, t0), metrics.counts(members, t0, t1), cfg, alpha)
    verdicts = [g["passed"] for g in out.values()]
    passed = False if False in verdicts else (True if True in verdicts else None)
    undecided = [n for n, g in out.items() if g["passed"] is None]
    return {"passed": passed, "groups": out, "undecided": undecided}


@dataclass
class Rollout:
    release: int
    waves: list[dict[str, Any]]
    gate_cfg: dict[str, Any] | None
    respect_windows: bool
    state: str = "pending"  # pending -> rolling -> completed | rolled_back
    wave: int = 0
    deployed: dict[str, float] = field(default_factory=dict)  # account -> when it finished
    started: dict[str, float] = field(default_factory=dict)  # account -> when its deploy began
    wave_of: dict[str, str] = field(default_factory=dict)
    wave_started: float = 0.0
    events: list[dict[str, Any]] = field(default_factory=list)

    def advance(
        self, now: float, accounts: dict[str, Account], ex: Executor, metrics: Metrics, epoch: datetime
    ) -> float | None:
        """Does what is due at `now`; returns the next time to wake, or None when finished."""
        if self.state in ("completed", "rolled_back"):
            return None
        if self.state == "pending":
            self.state, self.wave, self.wave_started = "rolling", 0, now
            self.events.append({"t": now, "event": "wave", "wave": self.waves[0]["name"]})
        waiting = []
        for w in self.waves[: self.wave + 1]:  # this wave, and accounts of earlier waves whose window opened
            for acc in w["account_ids"]:
                if acc in self.deployed:
                    continue
                a = accounts[acc]
                if self.respect_windows and not in_window(a, now, epoch):
                    waiting.append(next_window(a, now, epoch))
                    continue
                self.started[acc] = now
                self.wave_of[acc] = w["name"]
                self.deployed[acc] = ex.deploy(acc, self.release, now)
        w = self.waves[self.wave]
        ids = [a for a in w["account_ids"] if a in self.deployed]
        done_at = max((self.deployed[a] for a in ids), default=now)
        bake_end = done_at + w.get("bake_minutes", 0)
        if now < bake_end:
            return bake_end
        if self.gate_cfg and w.get("bake_minutes", 0) and ids:
            g = gate_wave(ids, accounts, metrics, self.wave_started - 60, done_at, now, self.gate_cfg)
            if (
                (g["passed"] is None or g["undecided"])
                and g["passed"] is not False
                and now - done_at < self.gate_cfg.get("max_bake_minutes", 0)
            ):
                return now + self.gate_cfg.get("extend_minutes", 15)  # not enough traffic yet: bake longer
            self.events.append({"t": now, "wave": w["name"], "gate": g})
            if g["passed"] is False:
                for acc in sorted(self.deployed):
                    ex.rollback(acc, self.release, now)
                self.state = "rolled_back"
                self.events.append({"t": now, "event": "rolled_back", "accounts": len(self.deployed)})
                return None
        if self.wave == len(self.waves) - 1:
            if waiting:
                return min(waiting)
            self.state = "completed"
            self.events.append({"t": now, "event": "completed"})
            return None
        self.wave += 1
        self.wave_started = now
        self.events.append({"t": now, "event": "wave", "wave": self.waves[self.wave]["name"]})
        return now

    def pending_restricted(self) -> list[str]:
        return [a for w in self.waves for a in w["account_ids"] if a not in self.deployed]

    def snapshot(self) -> dict[str, Any]:
        return {
            "release": self.release,
            "state": self.state,
            "wave": self.wave,
            "deployed": self.deployed,
            "started": self.started,
            "wave_of": self.wave_of,
            "events": self.events,
        }
