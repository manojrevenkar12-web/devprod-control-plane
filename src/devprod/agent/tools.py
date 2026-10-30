"""What the triage agent can look at and do. The same functions back the stand-in policy, the Claude
Agent SDK's in-process MCP server, and the standalone MCP server engineers use from Claude Code or
OpenCode (`devprod serve-mcp`). Reads are free; actions are recorded and limited by config/agent.yaml."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .. import settings
from ..ci.graph import Graph
from ..flaky.detector import FlakeRegister
from ..store import Store

PYTEST_FAILED = re.compile(r"^FAILED (\S+?)::(\S+?)(?: - (.*))?$", re.MULTILINE)
JEST_FILE = re.compile(r"^FAIL (\S+)$", re.MULTILINE)
JEST_TEST = re.compile(r"^\s+[✕×] (.+?)(?: \(\d+ ?ms\))?$", re.MULTILINE)


@dataclass
class Tools:
    store: Store
    graph: Graph
    flakes: FlakeRegister
    actions: list[dict[str, Any]] = field(default_factory=list)

    # ---- reads ----
    def failed_jobs(self, run_id: int) -> list[dict[str, Any]]:
        rows = self.store.rows(
            "select j.job_id, j.module, j.shard, j.step_failed, l.text from jobs j left join logs l using(job_id) "
            "where j.run_id=? and j.conclusion='failure'",
            (run_id,),
        )
        return [
            {**{k: r[k] for k in ("job_id", "module", "shard", "step_failed")}, "log_tail": (r["text"] or "")[-4000:]}
            for r in rows
        ]

    def failed_tests(self, log: str, module: str) -> list[str]:
        out = []
        for path, name, _ in PYTEST_FAILED.findall(log):
            m = self.graph.module_of(path) or module
            out.append(f"{m}::{name.split('[')[0]}")
        files = JEST_FILE.findall(log)
        if files:
            m = self.graph.module_of(files[0]) or module
            out += [f"{m}::{t.strip().replace(' ', '_')}" for t in JEST_TEST.findall(log)]
        return out

    def test_history(self, test_id: str) -> dict[str, Any]:
        return {**self.flakes.history(test_id), "quarantined": test_id in self.store.quarantined()}

    def pr_changes(self, pr: int) -> dict[str, Any]:
        r = self.store.rows("select modules, files, title from prs where pr=?", (pr,))
        if not r:
            return {"modules": [], "files": [], "affected": []}
        modules = json.loads(r[0]["modules"])
        return {
            "modules": modules,
            "files": json.loads(r[0]["files"]),
            "title": r[0]["title"],
            "affected": sorted(self.graph.affected(modules)),
        }

    def recent_step_failures(self, step: str, since: float, until: float) -> int:
        """Jobs of any pull request that failed at this step in the window (an outage shows here)."""
        r = self.store.rows(
            "select count(distinct r.pr) n from jobs j join runs r using(run_id) where j.step_failed=? "
            "and j.finished_ts between ? and ?",
            (step, since, until),
        )
        return int(r[0]["n"])

    # ---- actions (recorded; the CI executes them) ----
    def rerun_failed_jobs(self, run_id: int, reason: str) -> dict[str, Any]:
        a = {"action": "rerun_failed_jobs", "run_id": run_id, "reason": reason}
        self.actions.append(a)
        return a

    def comment(self, pr: int, ts: float, body: str, classification: str, run_id: int) -> None:
        self.store.insert(
            "comments",
            {
                "pr": pr,
                "ts": ts,
                "author": "triage-agent",
                "body": body,
                "classification": classification,
                "run_id": run_id,
            },
        )


def markers() -> dict[str, list[str]]:
    return settings.load("agent")["markers"]
