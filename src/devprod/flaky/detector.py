"""Which tests are flaky, from history alone.

The evidence is a *flip*: a test that failed and then passed on the same commit when its job was
rerun. A test that fails on a commit and keeps failing on reruns is broken (or the change broke it),
not flaky. Each test's flip rate gets a Beta posterior; a test is quarantined when it has flipped at
least `min_flips` times and the posterior says its rate is above 1% with 90% probability. A
quarantined test still runs and is recorded but does not block a merge; it gates again after
`graduate_after_clean_runs` passes in a row. The owning team is named and given a deadline.

Executions are counted per job (a module's shard), not per test, so the register keeps up with
monorepos running millions of test executions a week: a test's run count is its job's count since the
test was first seen, and only tests that have failed carry their own state."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

from scipy.stats import beta as beta_dist

from .. import settings
from ..store import Store


class FlakeRegister:
    def __init__(self, store: Store, owners: dict[str, str]) -> None:
        self.store = store
        self.owners = owners  # module -> team
        self.cfg = settings.load("flaky")
        self.execs: dict[str, int] = defaultdict(int)  # job key -> executions
        self.key_of: dict[str, str] = {}
        self.first: dict[str, int] = {}
        self.fails: dict[str, int] = defaultdict(int)
        self.flips: dict[str, int] = defaultdict(int)
        self.last_fail: dict[str, int] = {}
        self.pending: dict[tuple[str, str], set[str]] = defaultdict(set)  # (job key, sha) -> failed tests

    def observe_job(self, sha: str, key: str, tests: Sequence[str], failed: Iterable[str]) -> None:
        if key not in self.execs:
            for t in tests:
                self.key_of.setdefault(t, key)
                self.first.setdefault(t, 0)
        self.execs[key] += 1
        n = self.execs[key]
        failed = set(failed)
        for t in self.pending.pop((key, sha), set()) - failed:
            self.flips[t] += 1
        for t in failed:
            self.key_of.setdefault(t, key)
            self.fails[t] += 1
            self.last_fail[t] = n
        if failed:
            self.pending[(key, sha)] |= failed

    def runs(self, test: str) -> int:
        key = self.key_of.get(test)
        return self.execs[key] - self.first.get(test, 0) if key else 0

    def consecutive_passes(self, test: str) -> int:
        key = self.key_of.get(test)
        return self.execs[key] - self.last_fail.get(test, 0) if key else 0

    def p_flaky(self, test: str) -> float:
        if not self.flips.get(test):
            return 0.0
        pr, q = self.cfg["prior"], self.cfg["quarantine_when"]["posterior_p_rate_above"]
        passes = max(0, self.runs(test) - self.fails[test])
        return float(beta_dist.sf(q["rate"], pr["alpha"] + self.flips[test], pr["beta"] + passes))

    def should_quarantine(self, test: str) -> bool:
        q = self.cfg["quarantine_when"]
        return (
            self.flips.get(test, 0) >= q["min_flips"]
            and self.p_flaky(test) >= q["posterior_p_rate_above"]["probability"]
        )

    def review(self, now: float, touched_modules: set[str] | None = None) -> dict[str, list[str]]:
        """Quarantines and graduates; returns what changed."""
        current = self.store.quarantined()
        added, graduated = [], []
        for test in list(self.flips):
            module = test.split("::")[0]
            if test not in current and self.should_quarantine(test):
                if touched_modules and self.cfg["never_quarantine_when_touched"] and module in touched_modules:
                    continue
                self.store.upsert(
                    "quarantine",
                    {
                        "test_id": test,
                        "since_ts": now,
                        "until_ts": now + self.cfg["owner_sla_days"] * 24 * 60,
                        "owner": self.owners.get(module, "unowned"),
                        "reason": f"{self.flips[test]} flips in {self.runs(test)} runs",
                        "status": "quarantined",
                        "clean_runs": 0,
                    },
                )
                added.append(test)
        for test in current:
            if self.consecutive_passes(test) >= self.cfg["graduate_after_clean_runs"]:
                self.store.update(
                    "quarantine", "test_id", test, status="graduated", clean_runs=self.consecutive_passes(test)
                )
                graduated.append(test)
        return {"quarantined": added, "graduated": graduated}

    def history(self, test: str) -> dict[str, Any]:
        return {
            "test_id": test,
            "runs": self.runs(test),
            "fails": self.fails.get(test, 0),
            "flips": self.flips.get(test, 0),
            "p_flaky": round(self.p_flaky(test), 4),
        }

    def summary(self) -> list[dict[str, Any]]:
        return [self.history(t) for t in sorted(self.flips, key=lambda t: -self.flips[t])]


def replay(store: Store) -> FlakeRegister:
    """Rebuilds the register from the CI record (jobs in finishing order and their failed tests), so a
    fresh process (the MCP server, the API) sees the same history the CI saw."""
    owners = {m: v["owner"] for m, v in store.modules().items()}
    reg = FlakeRegister(store, owners)
    failed: dict[int, list[str]] = defaultdict(list)
    for r in store.rows("select job_id, test_id from test_results"):
        failed[r["job_id"]].append(r["test_id"])
    jobs = store.rows(
        "select j.job_id, j.module, j.shard, j.conclusion, r.sha from jobs j join runs r using(run_id) "
        "order by j.finished_ts, j.job_id"
    )
    for j in jobs:
        if j["conclusion"] != "success" and j["job_id"] not in failed:
            continue  # the runner or an install failed: the tests did not run
        reg.observe_job(j["sha"], f"{j['module']}#{j['shard']}", [], failed.get(j["job_id"], []))
    return reg
