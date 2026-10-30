"""Releases reaching customers. Merged changes leave on release trains; a release containing a
regression that escaped CI makes the accounts running it fail more decisions (truth). The deploy
engine of the policy rolls each release out; a person notices a bad release some time after it first
hurts a customer and rolls it back if no gate did. Exposure is measured as customer traffic times
minutes on a bad release."""

from __future__ import annotations

import bisect
import heapq
import json
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from .. import settings
from ..deploy.engine import Account, Rollout, plan
from ..store import Store
from .repo import rng
from .world import DAY, next_work, u


def accounts() -> list[Account]:
    cfg = settings.load("org")["customers"]
    r = rng("accounts")
    tiers = cfg["tiers"]
    syn = float(cfg["internal_synthetic_traffic"])
    out = [
        Account("internal-dev", "internal", "Europe/Berlin", "any", syn),
        Account("internal-staging", "internal", "Europe/Berlin", "any", syn),
    ]
    for k in range(cfg["accounts"]):
        tier = str(r.choice(list(tiers), p=np.array(list(tiers.values())) / sum(tiers.values())))
        tz = str(r.choice(cfg["timezones"]))
        window = "18-07" if r.random() < cfg["restricted_windows_share"] else "any"
        traffic = cfg["traffic"][tier] * float(np.exp(r.normal(0, 0.5)))
        out.append(Account(f"cust-{k + 1:04d}", tier, tz, window, round(traffic, 2)))
    return out


class Fleetwide:
    """Which release every account runs over time, and the metrics that follow from it."""

    def __init__(self, accs: list[Account], bad: dict[int, float]) -> None:
        self.accs = {a.account_id: a for a in accs}
        self.timeline: dict[str, list[tuple[float, int]]] = {a.account_id: [(0.0, 0)] for a in accs}
        self.bad: dict[int, dict[str, float]] = bad  # release -> tier -> error rate (truth)
        self.base_rate = settings.load("org")["customers"]["baseline_error_rate"]
        self.logged: list[dict[str, Any]] = []
        self.rollbacks: list[dict[str, Any]] = []

    def version(self, acc: str, t: float) -> int:
        tl = self.timeline[acc]
        k = bisect.bisect_right([x[0] for x in tl], t) - 1
        return tl[max(0, k)][1]

    def deploy(self, acc: str, release: int, at: float) -> float:
        end = at + 4 + 4 * u("deploy", acc, release)
        self.timeline[acc].append((end, release))
        self.timeline[acc].sort()
        return end

    def rollback(self, acc: str, release: int, at: float) -> float:
        tl = self.timeline[acc]
        tl[:] = [x for x in tl if not (x[0] > at and x[1] == release)]  # cancel a deploy still in flight
        prev = next((r for _, r in reversed(tl) if r != release and r not in self.bad), 0)
        end = at + 3 + 2 * u("rollback", acc, release)
        self.rollbacks.append({"release_id": release, "account_id": acc, "started_ts": at, "finished_ts": end})
        tl.append((end, prev))
        tl.sort()
        return end

    def rate(self, acc: str, release: int) -> float:
        return self.bad.get(release, {}).get(self.accs[acc].tier, self.base_rate)

    def hurts(self, acc: str, release: int) -> bool:
        return self.accs[acc].tier in self.bad.get(release, {})

    def counts(self, ids: list[str], t0: float, t1: float) -> tuple[int, int]:
        n = e = 0
        for acc in ids:
            a = self.accs[acc]
            tl = self.timeline[acc]
            points = sorted({t0, t1, *(x for x, _ in tl if t0 < x < t1)})
            for s0, s1 in pairwise(points):
                r = np.random.default_rng(int(u("m", acc, round(s0, 2), round(s1, 2)) * 2**32))
                req = int(r.poisson(a.traffic * (s1 - s0)))
                err = int(r.binomial(req, self.rate(acc, self.version(acc, (s0 + s1) / 2))))
                n += req
                e += err
        self.logged.append({"accounts": len(ids), "t0": t0, "t1": t1, "requests": n, "errors": e})
        return n, e

    def exposure(self, until: float) -> float:
        """Decisions run on a bad release (traffic x minutes)."""
        total = 0.0
        for acc, tl in self.timeline.items():
            a = self.accs[acc]
            if acc.startswith("internal-"):
                continue
            for (t, rel), nxt in zip(tl, [*tl[1:], (until, None)], strict=False):
                if self.hurts(acc, rel):
                    total += a.traffic * max(0.0, min(nxt[0], until) - t)
        return total


def run(policy: str, variant: str | None = None) -> dict[str, Any]:
    """Deploys the releases of `policy`'s CI world with the deploy settings of `variant` (default: the
    policy's own). A variant is an ablation: its results go to truth only, never to the observed store."""
    cfg = settings.load("org")
    dcfg = settings.load("deploy")[variant or policy]
    truth_prs = json.loads((settings.truth(policy) / "prs.json").read_text())
    store = Store(settings.observed(policy) if variant is None else ":memory:")
    for table in ("accounts", "releases", "deployments", "account_metrics"):
        store.db.execute(f"delete from {table}")  # the deploy phase can be rerun on its own
    accs = accounts()
    for a in accs:
        store.upsert(
            "accounts",
            {
                "account_id": a.account_id,
                "tier": a.tier,
                "timezone": a.timezone,
                "window": a.window,
                "traffic": a.traffic,
            },
        )
    epoch = datetime.fromisoformat(str(cfg["start"])).replace(tzinfo=ZoneInfo(cfg["timezone"]))
    end = cfg["weeks"] * 7 * DAY
    merged = sorted((p for p in truth_prs if p["merged"] is not None), key=lambda p: p["merged"])
    # the same escaped regression hurts the same customers equally under both policies
    error_of = {p["pr"]: harm(p["pr"]) for p in merged if p["escaped"]}
    lo, hi = cfg["customers"]["manual_detection_minutes"]
    fw = Fleetwide(accs, {})
    by_id = {a.account_id: a for a in accs}
    waves = plan(accs, dcfg["waves"])
    releases: list[dict[str, Any]] = []
    rollouts: list[Rollout] = []
    reverted: dict[int, float] = {}  # pr -> when it was reverted
    noticed: set[int] = set()
    by_person: set[int] = set()
    heap: list[tuple[float, int, str, Any]] = []
    seq = 0

    def push(t: float, kind: str, data: Any = None) -> None:
        nonlocal seq
        seq += 1
        heapq.heappush(heap, (t, seq, kind, data))

    t = next_work(0)
    while t < end:
        push(t, "train")
        t = next_work(t + cfg["releases"]["train_every_minutes"])
    active: Rollout | None = None
    waiting: int | None = None
    last_content = -1
    shipped = 0
    sha_of = {int(x["pr"]): x["sha"] for x in Store(settings.observed(policy)).rows("select pr, sha from commits")}

    def start(now: float) -> None:
        nonlocal active, waiting
        if waiting is not None and set(releases[waiting - 1]["bad_prs"]) & set(reverted):
            waiting = None  # it contains a change that has since been reverted; the next train will carry the fix
        if active is None and waiting is not None:
            active = Rollout(waiting, waves, dcfg["gate"], dcfg["respect_windows"])
            rollouts.append(active)
            waiting = None
            push(now, "advance", active)

    while heap:
        now, _, kind, data = heapq.heappop(heap)
        if now > end + 2 * DAY:
            break
        if kind == "train":
            content = [p["pr"] for p in merged if p["merged"] <= now]
            if len(content) == last_content:
                continue  # nothing new merged since the last train
            last_content = len(content)
            bad_prs = [p for p in content if p in error_of and p not in reverted]
            rate: dict[str, float] = {}
            for p in bad_prs:
                for tier, x in error_of[p].items():
                    rate[tier] = max(rate.get(tier, 0.0), x)
            k = len(releases) + 1
            releases.append({"release": k, "ts": now, "prs": len(content), "bad_prs": bad_prs, "rate": rate})
            if rate:
                fw.bad[k] = rate
            new = [sha_of[p] for p in content[shipped:]]
            shipped = len(content)
            store.insert("releases", {"release_id": k, "ts": now, "shas": new})
            waiting = k  # a newer release supersedes one still waiting
            start(now)
        elif kind == "advance":
            if data is not active:
                continue
            nw = active.advance(now, by_id, fw, fw, epoch)
            rel = active.release
            cust = [ts for a, ts in active.deployed.items() if fw.hurts(a, rel) and not a.startswith("internal-")]
            if rel in fw.bad and cust and rel not in noticed:
                noticed.add(rel)  # a person notices some time after it first hurts a customer
                push(min(cust) + lo + (hi - lo) * u("notice", rel), "notice", rel)
            if nw is None:
                if active.state == "rolled_back":
                    for p in releases[rel - 1]["bad_prs"]:
                        reverted.setdefault(p, now)
                active = None
                start(now)
            else:
                push(max(nw, now + 0.5), "advance", active)
        elif kind == "notice":
            prs = set(releases[data - 1]["bad_prs"])
            for p in prs:
                reverted.setdefault(p, now)
            hit = {x["release"] for x in releases if set(x["bad_prs"]) & prs}
            for acc, tl in fw.timeline.items():
                for rel in {fw.version(acc, now), *(v for ts, v in tl if ts > now)} & hit:
                    fw.rollback(acc, rel, now)
                    by_person.add(rel)
            if active is not None and active.release in hit:
                active.state = "rolled_back"
                active.events.append({"t": now, "event": "rolled_back_by_person"})
                active = None
            start(now)
    for ro in rollouts:
        for acc, ts in ro.deployed.items():
            store.insert(
                "deployments",
                {
                    "release_id": ro.release,
                    "account_id": acc,
                    "wave": ro.wave_of.get(acc, ""),
                    "started_ts": ro.started.get(acc, ts),
                    "finished_ts": ts,
                    "status": ro.state,
                },
            )
    for rb in fw.rollbacks:
        store.insert("deployments", {**rb, "wave": "rollback", "status": "rollback"})
    store.insert_many(
        "account_metrics",
        [
            {
                "account_id": f"{x['accounts']} accounts",
                "ts": x["t1"],
                "release_id": 0,
                "requests": x["requests"],
                "errors": x["errors"],
            }
            for x in fw.logged
        ],
    )
    store.commit()
    reach = []
    for ro in rollouts:
        cust = sorted(ts for a, ts in ro.deployed.items() if not a.startswith("internal-"))
        if ro.state == "completed" and cust:
            reach.append(float(np.quantile(cust, 0.95)) - releases[ro.release - 1]["ts"])
    gate_rb = [ro for ro in rollouts if any(e.get("event") == "rolled_back" for e in ro.events)]
    out = {
        "policy": policy,
        "deploy_settings": variant or policy,
        "releases": len(releases),
        "rollouts": len(rollouts),
        "completed": sum(1 for ro in rollouts if ro.state == "completed"),
        "bad_releases": len(fw.bad),
        "bad_rollouts": sum(1 for ro in rollouts if ro.release in fw.bad),
        "stopped_by_gate": sum(1 for ro in gate_rb if ro.release in fw.bad),
        "false_rollbacks": sum(1 for ro in gate_rb if ro.release not in fw.bad),
        "rolled_back_by_person": len(by_person),
        "customer_accounts_hit": sum(
            1 for a, tl in fw.timeline.items() if not a.startswith("internal-") and any(fw.hurts(a, v) for _, v in tl)
        ),
        "exposure_decision_minutes": round(fw.exposure(end), 0),
        "bad_decisions": round(sum(_bad_decisions(fw, a, end) for a in fw.accs), 0),
        "release_to_95pct_minutes_median": round(float(np.median(reach)), 1) if reach else None,
    }
    (settings.truth(policy) / f"deploys{'' if variant is None else '-' + variant}.json").write_text(
        json.dumps(
            {"summary": out, "releases": releases, "rollouts": [ro.snapshot() for ro in rollouts]}, default=float
        )
    )
    return out


def harm(pr: int) -> dict[str, float]:
    """Truth: which tiers an escaped regression hurts and how badly. The synthetic replay in the internal
    accounts exercises the paths every customer uses, not tier-only ones."""
    c = settings.load("org")["customers"]
    lo, hi = c["bad_release_error_rate"]
    rate = lo + (hi - lo) * u("harm", pr)
    scopes = list(c["regression_scope"])
    cum = np.cumsum(list(c["regression_scope"].values()))
    chosen = scopes[min(int(np.searchsorted(cum, u("scope", pr), side="right")), len(scopes) - 1)]
    tiers = [*c["tiers"], "internal"] if chosen == "all" else [chosen]
    return {t: rate for t in tiers}


def _bad_decisions(fw: Fleetwide, acc: str, until: float) -> float:
    """Extra failed decisions caused by bad releases on one account (truth)."""
    a = fw.accs[acc]
    if acc.startswith("internal-"):
        return 0.0
    tl = fw.timeline[acc]
    total = 0.0
    for (t, rel), nxt in zip(tl, [*tl[1:], (until, None)], strict=False):
        if fw.hurts(acc, rel):
            total += a.traffic * max(0.0, min(nxt[0], until) - t) * (fw.rate(acc, rel) - fw.base_rate)
    return total


def local_hour(epoch: datetime, t: float, tz: str) -> int:
    return (epoch + timedelta(minutes=t)).astimezone(ZoneInfo(tz)).hour
