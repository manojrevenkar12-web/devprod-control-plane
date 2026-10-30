"""How many self-hosted runners to have, per label set, from what GitHub says is queued.

The same rule the simulated fleet uses (config/ci.yaml: fleet.autoscale): enough runners that no job
waits longer than `target_queue_minutes`, never more than `max`, never fewer than `min` in working
hours, and idle runners are let go after `idle_shutdown_minutes`. Runners are ephemeral (one job, then
the instance is replaced), so a busy runner is never scaled in. The Lambda in infra/runners calls
`decide` on every `workflow_job` webhook and once a minute."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Pool:
    labels: tuple[str, ...]
    queued: int  # jobs waiting for these labels
    busy: int  # runners running a job
    idle: int  # runners up and waiting
    booting: int  # instances launched, runner not registered yet
    oldest_queued_minutes: float = 0.0


def decide(
    pool: Pool, cfg: dict[str, Any], working_hours: bool, idle_minutes: list[float] | None = None
) -> dict[str, Any]:
    """Desired instances for the pool and which idle runners to stop (by index into idle_minutes)."""
    a = cfg["autoscale"]
    floor = a["min"] if working_hours else 0
    supply = pool.idle + pool.booting
    want_new = max(0, pool.queued - supply)
    if pool.oldest_queued_minutes > a["target_queue_minutes"]:
        want_new = max(want_new, 1)  # something is waiting too long: add one even if a boot is in flight
    desired = min(a["max"], max(floor, pool.busy + supply + want_new))
    stop: list[int] = []
    if idle_minutes and pool.queued == 0:
        surplus = pool.busy + supply - max(floor, pool.busy)
        for i in sorted(range(len(idle_minutes)), key=lambda k: -idle_minutes[k]):
            if surplus <= 0 or idle_minutes[i] < a["idle_shutdown_minutes"]:
                break
            stop.append(i)
            surplus -= 1
        desired = max(floor, desired - len(stop))
    return {
        "labels": list(pool.labels),
        "desired": desired,
        "launch": max(0, desired - (pool.busy + supply)),
        "stop_idle": stop,
        "capped": pool.busy + supply + want_new > a["max"],
    }


def cost_per_build(runner_minutes: float, builds: int, eur_per_minute: float) -> float:
    return round(runner_minutes * eur_per_minute / builds, 4) if builds else math.nan
