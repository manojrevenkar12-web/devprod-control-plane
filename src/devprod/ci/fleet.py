"""The self-hosted runner fleet: a fixed pool, or an autoscaler that adds runners when jobs wait
longer than a target and removes runners idle beyond a limit. The same code decides in the
simulation and in production (infra/terraform/runners scales the EC2 Auto Scaling group from it)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Runner:
    name: str
    up_at: float
    free_at: float
    busy_minutes: float = 0.0
    down_at: float | None = None


@dataclass
class Fleet:
    mode: str
    cfg: dict[str, Any]
    runners: list[Runner] = field(default_factory=list)
    n: int = 0

    def __post_init__(self) -> None:
        start = self.cfg["fixed_runners"] if self.mode == "fixed" else self.cfg["autoscale"]["min"]
        for _ in range(start):
            self._add(0.0)

    def _add(self, at: float) -> Runner:
        self.n += 1
        r = Runner(f"runner-{self.n:03d}", at, at)
        self.runners.append(r)
        return r

    def alive(self, t: float) -> list[Runner]:
        return [r for r in self.runners if r.down_at is None or r.down_at > t]

    def run(self, ready: float, minutes: float) -> tuple[str, float, float]:
        """Schedules a job ready at `ready` for `minutes`; returns (runner, start, end)."""
        if self.mode != "fixed":
            self._scale_down(ready)
        alive = self.alive(ready)
        best = min(alive, key=lambda r: max(r.free_at, r.up_at))
        start = max(ready, best.free_at, best.up_at)
        if self.mode != "fixed":
            a = self.cfg["autoscale"]
            if start - ready > a["target_queue_minutes"] and len(alive) < a["max"]:
                new = self._add(ready + a["scale_up_minutes"])
                if new.up_at < start:
                    best, start = new, new.up_at
        best.free_at = start + minutes
        best.busy_minutes += minutes
        return best.name, start, start + minutes

    def _scale_down(self, t: float) -> None:
        a = self.cfg["autoscale"]
        alive = self.alive(t)
        idle = sorted((r for r in alive if r.free_at + a["idle_shutdown_minutes"] < t), key=lambda r: r.free_at)
        for r in idle:
            if len(self.alive(t)) <= a["min"]:
                break
            r.down_at = r.free_at + a["idle_shutdown_minutes"]

    def runner_minutes(self, end: float) -> float:
        return sum((min(r.down_at, end) if r.down_at is not None else end) - r.up_at for r in self.runners)

    def usage_by_day(self, end: float, day: float = 24 * 60) -> list[dict[str, float]]:
        """Billed runner minutes and the most runners alive, per day (what the cloud bill shows)."""
        out = []
        for d in range(int(end // day)):
            lo, hi = d * day, (d + 1) * day
            spans = [(max(lo, r.up_at), min(hi, r.down_at if r.down_at is not None else end)) for r in self.runners]
            spans = [(a, b) for a, b in spans if b > a]
            points = sorted({lo, *(a for a, _ in spans)})
            peak = max((sum(1 for a, b in spans if a <= t < b) for t in points), default=0)
            out.append({"day": d, "runner_minutes": round(sum(b - a for a, b in spans), 1), "peak_runners": peak})
        return out
