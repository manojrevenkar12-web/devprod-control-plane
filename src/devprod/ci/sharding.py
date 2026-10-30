"""Duration-balanced test shards from history: longest test first into the least-loaded shard
(LPT), with as many shards as it takes to reach the target duration, capped."""

from __future__ import annotations

import heapq
import math


def shard(durations: dict[str, float], target_minutes: float | None, max_shards: int) -> list[list[str]]:
    tests = sorted(durations, key=lambda t: (-durations[t], t))
    if not tests:
        return [[]]
    total_min = sum(durations.values()) / 60
    n = 1 if not target_minutes else max(1, min(max_shards, math.ceil(total_min / target_minutes)))
    heap = [(0.0, k) for k in range(n)]
    out: list[list[str]] = [[] for _ in range(n)]
    for t in tests:
        load, k = heapq.heappop(heap)
        out[k].append(t)
        heapq.heappush(heap, (load + durations[t], k))
    return out


def balance(shards: list[list[str]], durations: dict[str, float]) -> float:
    """Longest shard over the mean; 1.0 is perfect."""
    loads = [sum(durations[t] for t in s) for s in shards]
    mean = sum(loads) / len(loads)
    return max(loads) / mean if mean else 1.0
