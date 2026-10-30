"""Pipeline tests build into build/test (and write reports there), so `pytest` never touches the
committed reports. Unit tests use the small store below."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("DEVPROD_BUILD", str(ROOT / "build" / "test"))
os.environ.setdefault("DEVPROD_REPORTS", str(ROOT / "build" / "test" / "reports"))

import pytest  # noqa: E402

from devprod.store import Store  # noqa: E402

MODULES = {
    "core": ("python", [], "platform", "libs/core"),
    "schemas": ("python", ["core"], "platform", "libs/schemas"),
    "api": ("python", ["schemas"], "decisions", "services/api"),
    "billing": ("python", ["core"], "data", "services/billing"),
    "ui-kit": ("typescript", [], "frontend", "frontend/ui-kit"),
    "web": ("typescript", ["ui-kit"], "frontend", "frontend/web"),
}


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "observed.sqlite", fresh=True)
    for name, (lang, deps, owner, path) in MODULES.items():
        s.insert("modules", {"name": name, "language": lang, "deps": deps, "owner": owner, "path": path})
        s.insert_many(
            "tests", [{"test_id": f"{name}::test_{k}", "module": name, "seconds": 0.5 + k % 7} for k in range(40)]
        )
    s.commit()
    return s


def add_pr(s: Store, pr: int, modules: list[str], files: list[str] | None = None) -> None:
    files = files or [f"{MODULES[m][3]}/x.py" for m in modules]
    s.insert(
        "prs",
        {
            "pr": pr,
            "author": f"dev{pr % 5}",
            "team": "t",
            "opened_ts": 0.0,
            "merged_ts": None,
            "modules": modules,
            "files": files,
            "title": f"change {pr}",
            "head_sha": f"sha{pr}",
        },
    )


def add_failed_run(s: Store, run_id: int, pr: int, module: str, log: str, t: float = 100.0, step: str = "test") -> None:
    s.insert(
        "runs",
        {
            "run_id": run_id,
            "pr": pr,
            "sha": f"sha{pr}",
            "attempt": 1,
            "trigger": "push",
            "started_ts": t - 10,
            "finished_ts": t,
            "conclusion": "failure",
        },
    )
    job = s.insert(
        "jobs",
        {
            "run_id": run_id,
            "module": module,
            "shard": 0,
            "runner": "r1",
            "queued_ts": t - 10,
            "started_ts": t - 10,
            "finished_ts": t,
            "conclusion": "failure",
            "step_failed": step,
            "minutes": 10,
            "cache_hit": 1,
        },
    )
    s.insert("logs", {"job_id": job, "text": log})
