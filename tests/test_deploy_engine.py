"""Progressive delivery: wave planning, windows, the gate, rollback, resumability."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from devprod import settings
from devprod.deploy.engine import Account, Rollout, gate, gate_wave, in_window, next_window, plan

EPOCH = datetime(2026, 9, 7, 0, 0, tzinfo=UTC)  # a Monday, midnight UTC


def fleet(n: int = 60) -> list[Account]:
    tiers = ["starter", "growth", "enterprise"]
    out = [
        Account("internal-dev", "internal", "UTC", "any", 30),
        Account("internal-staging", "internal", "UTC", "any", 30),
    ]
    out += [Account(f"c{k:03d}", tiers[k % 3], "UTC", "any", [1, 8, 40][k % 3]) for k in range(n)]
    return out


class World:
    """A fake Executor and Metrics: each account serves `traffic` decisions a minute at an error
    rate that depends on its release (bad releases hurt the tiers in `hurts`)."""

    def __init__(self, accounts: list[Account], bad: set[int], hurts: set[str] | None = None) -> None:
        self.acc = {a.account_id: a for a in accounts}
        self.on: dict[str, list[tuple[float, int]]] = {a: [(-1e9, 0)] for a in self.acc}
        self.bad, self.hurts = bad, hurts
        self.rollbacks: list[str] = []

    def deploy(self, account: str, release: int, at: float) -> float:
        self.on[account].append((at + 5, release))
        return at + 5

    def rollback(self, account: str, release: int, at: float) -> float:
        self.rollbacks.append(account)
        self.on[account].append((at + 3, 0))
        return at + 3

    def release_at(self, account: str, t: float) -> int:
        return [r for ts, r in self.on[account] if ts <= t][-1]

    def counts(self, ids: list[str], t0: float, t1: float) -> tuple[int, int]:
        n = e = 0
        for a in ids:
            req = int(self.acc[a].traffic * (t1 - t0))
            r = self.release_at(a, (t0 + t1) / 2)
            hit = r in self.bad and (
                self.hurts is None
                or self.acc[a].tier in self.hurts
                or (self.acc[a].tier == "internal" and self.hurts is None)
            )
            n += req
            e += round(req * (0.08 if hit else 0.002))
        return n, e


def run(ro: Rollout, accounts: list[Account], w: World, until: float = 5000, start: float = 0.0) -> Rollout:
    by_id = {a.account_id: a for a in accounts}
    t = start
    while t < until:
        nxt = ro.advance(t, by_id, w, w, EPOCH)
        if nxt is None:
            return ro
        t = max(nxt, t + 0.5)
    raise AssertionError(f"rollout did not finish: {ro.state} at wave {ro.wave}")


def cp() -> dict:
    return settings.load("deploy")["control-plane"]


def test_plan_puts_every_account_in_one_wave_and_stratifies():
    accs = fleet()
    waves = plan(accs, cp()["waves"])
    ids = [a for w in waves for a in w["account_ids"]]
    assert sorted(ids) == sorted(a.account_id for a in accs)
    canary = {a.account_id: a for a in accs}
    tiers = {canary[a].tier for a in waves[2]["account_ids"]}
    assert tiers == {"starter", "growth", "enterprise"}  # every tier, early
    assert len(waves[2]["account_ids"]) <= 6


def test_unstratified_canary_takes_the_quietest_starters():
    accs = fleet()
    waves = plan(accs, settings.load("deploy")["control-plane-pooled"]["waves"])
    by = {a.account_id: a for a in accs}
    assert {by[a].tier for a in waves[2]["account_ids"]} == {"starter"}


def test_windows_wrap_midnight():
    a = Account("x", "growth", "Europe/Berlin", "18-07", 1)
    assert not in_window(a, 12 * 60, EPOCH)  # 14:00 in Berlin
    assert in_window(a, 22 * 60, EPOCH)  # 00:00 in Berlin
    assert in_window(a, 4 * 60, EPOCH)  # 06:00 in Berlin
    assert next_window(a, 12 * 60, EPOCH) == 16 * 60  # 18:00 Berlin = 16:00 UTC


def test_gate_fails_only_on_a_significant_rise():
    g = cp()["gate"]
    assert gate((10000, 20), (1000, 2), g)["passed"] is True
    assert gate((10000, 20), (1000, 30), g)["passed"] is False
    assert gate((10000, 20), (100, 50), g)["passed"] is None  # too little traffic to decide
    assert gate((10000, 20), (1000, 6), g)["passed"] is True  # 3x but not significant at 1000 requests


def test_good_release_reaches_everyone():
    accs = fleet()
    w = World(accs, bad=set())
    ro = run(Rollout(1, plan(accs, cp()["waves"]), cp()["gate"], True), accs, w)
    assert ro.state == "completed" and len(ro.deployed) == len(accs) and not w.rollbacks


def test_bad_release_is_stopped_in_dev_and_never_reaches_a_customer():
    accs = fleet()
    w = World(accs, bad={1})
    ro = run(Rollout(1, plan(accs, cp()["waves"]), cp()["gate"], True), accs, w)
    assert ro.state == "rolled_back"
    assert set(ro.deployed) == {"internal-dev"} and w.rollbacks == ["internal-dev"]


def test_enterprise_only_fault_is_caught_per_tier_in_the_canary():
    accs = fleet()
    w = World(accs, bad={1}, hurts={"enterprise"})
    ro = run(Rollout(1, plan(accs, cp()["waves"]), cp()["gate"], True), accs, w)
    assert ro.state == "rolled_back" and ro.waves[ro.wave]["name"] == "canary"
    customers = [a for a in ro.deployed if not a.startswith("internal-")]
    assert len(customers) <= 6 and set(w.rollbacks) == set(ro.deployed)
    failed = [e for e in ro.events if "gate" in e and e["gate"]["passed"] is False]
    assert failed and failed[0]["gate"]["groups"]["enterprise"]["passed"] is False


def test_pooled_gate_misses_a_fault_its_canary_does_not_exercise():
    accs = fleet()
    pooled = settings.load("deploy")["control-plane-pooled"]
    w = World(accs, bad={1}, hurts={"enterprise"})
    ro = run(Rollout(1, plan(accs, pooled["waves"]), pooled["gate"], True), accs, w)
    canary = next(e for e in ro.events if e.get("wave") == "canary" and "gate" in e)
    # three quiet starters: not enough traffic to decide anything, and nothing they run is broken
    assert canary["gate"]["passed"] is not False


def test_gate_wave_extends_until_every_tier_has_traffic():
    accs = fleet()
    w = World(accs, bad=set())
    by = {a.account_id: a for a in accs}
    g = gate_wave(["c000", "c002"], by, w, -60, 0, 10, {**cp()["gate"], "min_requests": 300})
    assert g["undecided"] == ["starter"] and g["passed"] is True


def test_restricted_accounts_wait_for_their_window():
    accs = [
        Account("internal-dev", "internal", "UTC", "any", 30),
        Account("internal-staging", "internal", "UTC", "any", 30),
        Account("night", "growth", "Europe/Berlin", "18-07", 8),
        Account("day", "growth", "UTC", "any", 8),
    ]
    waves = [{"name": "all", "share": 1.0, "bake_minutes": 0, "account_ids": ["night", "day"]}]
    w = World(accs, bad=set())
    ro = run(Rollout(1, waves, None, True), accs, w, start=10 * 60)  # 12:00 in Berlin
    assert ro.started["day"] == 600 and ro.started["night"] == 16 * 60  # 18:00 in Berlin


def test_snapshot_round_trip_resumes_where_it_stopped():
    accs = fleet()
    w = World(accs, bad=set())
    by = {a.account_id: a for a in accs}
    ro = Rollout(1, plan(accs, cp()["waves"]), cp()["gate"], True)
    t = ro.advance(0, by, w, w, EPOCH)
    again = Rollout.from_snapshot(ro.snapshot())
    assert again == ro
    assert again.advance(t, by, w, w, EPOCH) == ro.advance(t, by, w, w, EPOCH)


@pytest.mark.parametrize("policy", ["baseline", "control-plane"])
def test_policy_settings_load(policy):
    assert settings.load("deploy")[policy]["waves"]
