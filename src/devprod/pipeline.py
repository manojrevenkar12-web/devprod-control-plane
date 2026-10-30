"""`devprod run`: both worlds, their deploys, the ablation, the metrics, the evaluation, the reports and
the release gate."""

from __future__ import annotations

import json
import operator
import time
from typing import Any

from . import evaluation, settings
from .sim import deploys, world

OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge, "==": operator.eq}


def simulate(log: Any = print) -> dict[str, float]:
    took = {}
    for policy in settings.POLICIES:
        t = time.perf_counter()
        world.World(policy).run()
        deploys.run(policy)
        took[policy] = round(time.perf_counter() - t, 1)
        log(f"{policy}: simulated in {took[policy]} s")
    for variant in settings.load("deploy"):
        if variant not in settings.POLICIES:
            deploys.run("control-plane", variant)
    return took


def get(data: dict[str, Any], path: str) -> Any:
    x: Any = data
    for part in path.split("."):
        x = x[part]
    return x


def gate(results: dict[str, Any]) -> list[dict[str, Any]]:
    cfg = settings.load("release")
    subst = {"cp": cfg["cp"], "base": cfg["base"]}
    out = []
    for c in cfg["checks"]:
        left = get(results, c["left"].format(**subst))
        right = c["right"]
        right = get(results, right.format(**subst)) if isinstance(right, str) else right
        bound = right * c.get("factor", 1) if right is not None else None
        ok = left is not None and bound is not None and OPS[c["op"]](left, bound)
        out.append({"name": c["name"], "passed": bool(ok), "value": left, "op": c["op"], "bound": bound})
    return out


def run(log: Any = print, simulate_first: bool = True) -> dict[str, Any]:
    if simulate_first:
        simulate(log)
    results = evaluation.evaluate()
    results["gate"] = gate(results)
    from . import heldout

    sets = (
        sorted(p.name for p in settings.HELDOUT.iterdir() if (p / "cases.yaml").exists())
        if settings.HELDOUT.exists()
        else []
    )
    if sets:
        results["heldout"] = "\n\n".join(heldout.summary(s) for s in sets)
    settings.REPORTS.mkdir(parents=True, exist_ok=True)
    (settings.REPORTS / "results.json").write_text(json.dumps(results, indent=2, default=float))
    from . import report

    (settings.REPORTS / "RESULTS.md").write_text(report.markdown(results))
    return results
