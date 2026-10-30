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

# pytest: the short summary (`FAILED path::test - why`, `ERROR path::test` for setup errors) and
# pytest-xdist progress lines (`[gw3] [ 61%] FAILED path::test`); class-based ids keep the last part.
PYTEST = re.compile(r"(?:^|\]\s)(?:FAILED|ERROR)\s+(\S+?\.py)::(\S+)", re.MULTILINE)
# a test module that failed to import: `ERROR path/test_x.py` with no test after it
PYTEST_COLLECT = re.compile(r"^ERROR\s+(\S+?/(test_\w+|\w+_test)\.py)\s*(?:-.*)?$", re.MULTILINE)
JS_RUN = re.compile(r"^\s*(?:Test Files|Tests:|Test Suites:)", re.MULTILINE)
# Jest: `FAIL path (12.4 s)` then `● Suite › case`; Vitest: `FAIL  path > Suite > case` and `× case 41ms`.
JS_FILE = re.compile(r"^\s*FAIL\s+(\S+\.(?:test|spec)\.[cm]?[jt]sx?)", re.MULTILINE)
JEST_CASE = re.compile(r"^\s*● (.+?)\s*$", re.MULTILINE)
VITEST_CASE = re.compile(r"^\s*FAIL\s+\S+\.(?:test|spec)\.[cm]?[jt]sx?\s+>\s+(.+?)\s*$", re.MULTILINE)
CROSS_CASE = re.compile(r"^\s+[✕×] (.+?)(?:\s+\(?\d+(?:\.\d+)? ?m?s\)?)?\s*$", re.MULTILINE)
IN_PROGRESS = re.compile(r"(?:❯|RUNS)\s+(\S+\.(?:test|spec)\.[cm]?[jt]sx?|\S+\.py)", re.MULTILINE)


def _js_name(case: str) -> str:
    leaf = re.split(r"\s+[›>]\s+", case.strip())[-1]
    return re.sub(r"\s+", "_", leaf.strip())


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
        """Failing test ids in a log, `<module>::<name>`, in order and without repeats."""
        out: list[str] = []
        for path, name in PYTEST.findall(log):
            m = self.graph.module_of(path) or module
            out.append(f"{m}::{name.split('::')[-1].split('[')[0]}")
        for path, stem in PYTEST_COLLECT.findall(log):
            out.append(f"{self.graph.module_of(path) or module}::{stem}")
        files = JS_FILE.findall(log)
        if files or JS_RUN.search(log):  # only inside a Jest or Vitest run (pip also prints `×`)
            m = (self.graph.module_of(files[0]) if files else None) or module
            cases = [c for c in JEST_CASE.findall(log) if not c.startswith("Test suite failed to run")]
            cases += VITEST_CASE.findall(log) + CROSS_CASE.findall(log)
            out += [f"{m}::{_js_name(c)}" for c in cases]
        return list(dict.fromkeys(out))

    def in_progress(self, log: str, module: str) -> str | None:
        """The test file a job was still running when it was cut off (a hang or a runner death)."""
        hits = IN_PROGRESS.findall(log)
        return hits[-1] if hits else None

    def touches_manifest(self, pr: int) -> list[str]:
        """Dependency manifests and lock files the pull request changes."""
        names = markers()["manifests"]
        return [f for f in self.pr_changes(pr)["files"] if f.rsplit("/", 1)[-1] in names]

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
