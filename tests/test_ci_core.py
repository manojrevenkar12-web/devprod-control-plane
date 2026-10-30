"""Module graph, sharding, fleet and scaler."""

from __future__ import annotations

import math

from hypothesis import given, settings
from hypothesis import strategies as st

from devprod import settings as cfg
from devprod.ci.fleet import Fleet
from devprod.ci.graph import Graph
from devprod.ci.scaler import Pool, cost_per_build, decide
from devprod.ci.sharding import balance, shard


def test_affected_includes_transitive_dependents(store):
    g = Graph(store.modules())
    assert g.affected(["core"]) == {"core", "schemas", "api", "billing"}
    assert g.affected(["schemas"]) == {"schemas", "api"}
    assert g.affected(["ui-kit"]) == {"ui-kit", "web"}
    assert g.affected(["nope"]) == set()


def test_module_of_takes_the_longest_prefix(store):
    mods = store.modules()
    mods["core-x"] = {**mods["core"], "name": "core-x", "path": "libs/core/x"}
    g = Graph(mods)
    assert g.module_of("libs/core/x/a.py") == "core-x"
    assert g.module_of("libs/core/a.py") == "core"
    assert g.module_of("libs/corey/a.py") is None
    assert g.affected_by_files(["frontend/ui-kit/a.tsx", "README.md"]) == {"ui-kit", "web"}


def test_order_is_dependencies_first(store):
    order = Graph(store.modules()).order()
    assert order.index("core") < order.index("schemas") < order.index("api")


@settings(max_examples=60, deadline=None)
@given(
    st.dictionaries(st.text("abcdefgh", min_size=1, max_size=6), st.floats(0.01, 120), min_size=1, max_size=80),
    st.floats(0.5, 20),
    st.integers(1, 12),
)
def test_sharding_assigns_every_test_once_and_balances(durations, target, max_shards):
    shards = shard(durations, target, max_shards)
    flat = [t for s in shards for t in s]
    assert sorted(flat) == sorted(durations)
    assert 1 <= len(shards) <= max_shards
    loads = [sum(durations[t] for t in s) for s in shards]
    # LPT: no shard exceeds the mean by more than the longest test
    assert max(loads) <= sum(loads) / len(loads) + max(durations.values()) + 1e-9


def test_sharding_targets_minutes():
    d = {f"t{i}": 30.0 for i in range(60)}  # 30 minutes in total
    assert len(shard(d, 6, 8)) == 5
    assert len(shard(d, 6, 3)) == 3
    assert len(shard(d, None, 8)) == 1
    assert math.isclose(balance(shard(d, 6, 8), d), 1.0)


def fleet_cfg():
    return cfg.load("ci")["fleet"]


def test_fixed_fleet_queues_when_full():
    f = Fleet("fixed", {**fleet_cfg(), "fixed_runners": 2})
    starts = [f.run(0, 10)[1] for _ in range(3)]
    assert starts == [0, 0, 10]


def test_autoscale_adds_runners_and_lets_idle_ones_go():
    c = fleet_cfg()
    f = Fleet("autoscale", {**c, "autoscale": {**c["autoscale"], "min": 1, "max": 3}})
    jobs = [f.run(0, 30) for _ in range(5)]
    assert len(f.runners) == 3  # capped at max
    assert jobs[1][1] == c["autoscale"]["scale_up_minutes"]  # waited for a new runner, not for the busy one
    f.run(500, 1)  # long after: idle runners went down
    assert len(f.alive(500)) == 1
    usage = f.usage_by_day(1440)
    assert usage[0]["peak_runners"] == 3 and usage[0]["runner_minutes"] < 3 * 1440


def test_scaler_launches_for_queued_jobs_and_caps():
    c = fleet_cfg()
    d = decide(Pool(("ci-python",), queued=5, busy=2, idle=1, booting=1), c, working_hours=True)
    assert d["launch"] == 3 and d["desired"] == 7
    d = decide(Pool(("ci-python",), queued=500, busy=10, idle=0, booting=0), c, True)
    assert d["desired"] == c["autoscale"]["max"] and d["capped"]


def test_scaler_adds_one_when_a_job_waits_too_long():
    c = fleet_cfg()
    d = decide(Pool(("x",), queued=1, busy=12, idle=0, booting=1, oldest_queued_minutes=9), c, True)
    assert d["launch"] == 1


def test_scaler_stops_only_long_idle_runners_and_keeps_the_floor():
    c = fleet_cfg()
    a = c["autoscale"]
    idle = [a["idle_shutdown_minutes"] + 5, 1.0, a["idle_shutdown_minutes"] + 30]
    d = decide(Pool(("x",), queued=0, busy=a["min"], idle=3, booting=0), c, True, idle)
    assert d["stop_idle"] == [2, 0]  # the longest idle first, never the one idle for a minute
    night = decide(Pool(("x",), queued=0, busy=0, idle=3, booting=0), c, False, [99, 99, 99])
    assert night["desired"] == 0 and len(night["stop_idle"]) == 3
    assert cost_per_build(600, 10, 0.008) == 0.48
    assert math.isnan(cost_per_build(1, 0, 1))
