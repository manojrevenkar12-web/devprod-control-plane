"""How well the platform did, against the truth of the simulated organisation. Only this module and
`devprod.sim` may read build/<world>/truth or config/org.yaml.

Both worlds share their randomness by key (the same test flakes on the same commit and attempt, the
same change is a regression), so differences between them come from the platform, not the dice."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from . import settings
from .metrics import devprod
from .store import Store

KINDS = ("infra", "dependency", "flaky", "regression")


def _truth(policy: str, name: str) -> Any:
    return json.loads((settings.truth(policy) / f"{name}.json").read_text())


def ci(policy: str) -> dict[str, Any]:
    prs = _truth(policy, "prs")
    tests = _truth(policy, "tests")
    failures = _truth(policy, "failures")
    human = _truth(policy, "human")
    store = Store(settings.observed(policy))
    regressions = [p for p in prs if p["regression"]]
    # one incident per failure a person or the agent had to explain (a confirming rerun is part of its origin)
    incidents = [f for f in failures if "confirming_of" not in f]
    by_kind = Counter(f["kind"] for f in incidents)
    out: dict[str, Any] = {
        "regressions": len(regressions),
        "regressions_caught": sum(1 for p in regressions if p["caught"]),
        "regressions_escaped": sum(1 for p in regressions if p["escaped"]),
        "regressions_hidden_by_quarantine": sum(1 for p in regressions if p["hidden_by_quarantine"]),
        "failure_incidents": dict(sorted(by_kind.items())),
        "human_minutes": round(sum(h["minutes"] for h in human), 0),
        "human_minutes_by_kind": {k: round(sum(h["minutes"] for h in human if h["kind"] == k), 0) for k in KINDS},
    }
    judged = [f for f in incidents if "agent" in f]
    if judged:
        confusion = {k: Counter(f["agent"] for f in judged if f["kind"] == k) for k in KINDS}
        out["agent"] = {
            "judged": len(judged),
            "accuracy": round(sum(1 for f in judged if f["agent"] == f["kind"]) / len(judged), 3),
            "regression_precision": _ratio(
                sum(1 for f in judged if f["agent"] == f["kind"] == "regression"),
                sum(1 for f in judged if f["agent"] == "regression"),
            ),
            "regression_recall": _ratio(
                sum(1 for f in judged if f["agent"] == f["kind"] == "regression"),
                sum(1 for f in judged if f["kind"] == "regression"),
            ),
            "confusion": {k: dict(v) for k, v in confusion.items()},
        }
    # flaky-test detection: quarantined (ever) against truly flaky tests
    q = {r["test_id"] for r in store.rows("select test_id from quarantine")}
    flaky = set(tests)
    seen_failing = {r["test_id"] for r in store.rows("select distinct test_id from test_results")}
    detectable = flaky & seen_failing
    out["flake_detector"] = {
        "quarantined": len(q),
        "truly_flaky": len(flaky),
        "truly_flaky_seen_failing": len(detectable),
        "precision": _ratio(len(q & flaky), len(q)),
        "recall_of_seen_failing": _ratio(len(q & detectable), len(detectable)),
        "recall_of_flake_rate_ge_2pct": _ratio(
            len({t for t in q if tests.get(t, {}).get("flake_rate", 0) >= 0.02}),
            sum(1 for t in tests.values() if t["flake_rate"] >= 0.02),
        ),
        "wrongly_quarantined": sorted(q - flaky),
    }
    return out


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 3) if b else None


def deploys(policy: str, variant: str | None = None) -> dict[str, Any]:
    return _truth(policy, "deploys" if variant is None else f"deploys-{variant}")["summary"]


def evaluate() -> dict[str, Any]:
    worlds = {}
    for policy in settings.POLICIES:
        observed = devprod.compute(Store(settings.observed(policy)))
        worlds[policy] = {
            "observed": observed,
            "truth": {"ci": ci(policy), "deploys": deploys(policy)},
            "summary": _truth(policy, "summary"),
        }
    ablations = {}
    for variant in settings.load("deploy"):
        if variant not in settings.POLICIES and (settings.truth("control-plane") / f"deploys-{variant}.json").exists():
            ablations[variant] = deploys("control-plane", variant)
    return {"worlds": worlds, "ablations": ablations}
