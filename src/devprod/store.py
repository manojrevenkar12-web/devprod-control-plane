"""What GitHub, the CI and AWS hold about the engineering organisation: modules, pull requests,
workflow runs and jobs with their logs, test results, comments, the quarantine register, releases,
deployments and per-account metrics. SQLite for a world; PostgreSQL in production (sql/devprod.sql)."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
create table if not exists modules (name text primary key, language text, deps text, owner text, path text);
create table if not exists tests (test_id text primary key, module text, seconds real);
create table if not exists prs (pr integer primary key, author text, team text, opened_ts real, merged_ts real,
  modules text, files text, title text, head_sha text);
create table if not exists runs (run_id integer primary key, pr integer, sha text, attempt integer, trigger text,
  started_ts real, finished_ts real, conclusion text);
create index if not exists runs_pr on runs(pr);
create table if not exists jobs (job_id integer primary key, run_id integer, module text, shard integer,
  runner text, queued_ts real, started_ts real, finished_ts real, conclusion text, step_failed text,
  minutes real, cache_hit integer);
create index if not exists jobs_run on jobs(run_id);
create table if not exists test_results (job_id integer, run_id integer, sha text, test_id text, outcome text,
  seconds real, ts real);
create index if not exists results_test on test_results(test_id);
create index if not exists results_run on test_results(run_id);
create table if not exists logs (job_id integer primary key, text text);
create table if not exists comments (pr integer, ts real, author text, body text, classification text,
  run_id integer);
create table if not exists quarantine (test_id text primary key, since_ts real, until_ts real, owner text,
  reason text, status text, clean_runs integer);
create table if not exists commits (sha text primary key, pr integer, merged_ts real);
create table if not exists releases (release_id integer primary key, ts real, shas text);
create table if not exists accounts (account_id text primary key, tier text, timezone text, window text,
  traffic real);
create table if not exists deployments (release_id integer, account_id text, wave text, started_ts real,
  finished_ts real, status text);
create table if not exists account_metrics (account_id text, ts real, release_id integer, requests integer,
  errors integer);
create index if not exists metrics_acc on account_metrics(account_id, ts);
create table if not exists fleet_usage (day integer primary key, runner_minutes real, peak_runners integer,
  queue_minutes_mean real);
create table if not exists engineer_time (pr integer, run_id integer, ts real, minutes real, kind text);
"""


class Store:
    def __init__(self, path: Path | str, fresh: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if fresh and self.path.exists():
            self.path.unlink()
        self._conns: list[sqlite3.Connection] = []
        self._local = threading.local()
        self.db.executescript(SCHEMA)

    @property
    def db(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, check_same_thread=False)
            c.row_factory = sqlite3.Row
            self._local.conn = c
            self._conns.append(c)
        return c

    def close(self) -> None:
        for c in self._conns:
            c.close()
        self._conns.clear()
        self._local = threading.local()

    def __del__(self) -> None:
        for c in getattr(self, "_conns", []):
            c.close()

    def rows(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def insert(self, table: str, row: dict[str, Any]) -> int:
        cols = list(row)
        vals = [json.dumps(v) if isinstance(v, (list, dict)) else v for v in row.values()]
        cur = self.db.execute(f"insert into {table} ({','.join(cols)}) values ({','.join('?' * len(cols))})", vals)
        return int(cur.lastrowid or 0)

    def insert_many(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        cols = list(rows[0])
        self.db.executemany(
            f"insert into {table} ({','.join(cols)}) values ({','.join('?' * len(cols))})",
            [[json.dumps(r[c]) if isinstance(r[c], (list, dict)) else r[c] for c in cols] for r in rows],
        )

    def upsert(self, table: str, row: dict[str, Any]) -> None:
        cols = list(row)
        self.db.execute(
            f"insert or replace into {table} ({','.join(cols)}) values ({','.join('?' * len(cols))})",
            [json.dumps(v) if isinstance(v, (list, dict)) else v for v in row.values()],
        )

    def update(self, table: str, key: str, value: Any, **fields: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"update {table} set {sets} where {key}=?", [*fields.values(), value])

    def commit(self) -> None:
        self.db.commit()

    # common reads
    def modules(self) -> dict[str, dict[str, Any]]:
        return {r["name"]: {**r, "deps": json.loads(r["deps"])} for r in self.rows("select * from modules")}

    def tests_of(self, module: str) -> list[str]:
        return [r["test_id"] for r in self.rows("select test_id from tests where module=?", (module,))]

    def quarantined(self, at: float | None = None) -> set[str]:
        rows = self.rows("select test_id from quarantine where status='quarantined'")
        return {r["test_id"] for r in rows}
