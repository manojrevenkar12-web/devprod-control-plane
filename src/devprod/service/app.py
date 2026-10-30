"""The control plane's HTTP service: GitHub webhooks in, metrics and diagnoses out.

- `POST /webhooks/github` checks `X-Hub-Signature-256` (HMAC-SHA256 of the raw body with the webhook
  secret) before parsing anything, and records each `X-GitHub-Delivery` once, so GitHub's redeliveries
  are acknowledged without acting twice. A failed `workflow_run` is triaged; a `pull_request` gets the
  list of module workflows the change needs (what ci-gate waits for). There is no default secret: the
  service refuses to start without `DEVPROD_WEBHOOK_SECRET`.
- `GET /api/...` serves the dashboard: CI and delivery metrics from the observed store, flaky tests,
  a run's diagnosis, and the generated evaluation report."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from .. import settings
from ..agent.tools import Tools
from ..agent.triage import StandIn, as_dict
from ..ci.graph import Graph
from ..ci.workflows import required_workflows
from ..flaky.detector import replay
from ..metrics import devprod
from ..store import Store


def signature_ok(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    want = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, header)


class World:
    """One observed store and what is derived from it, loaded once."""

    def __init__(self, path: Path) -> None:
        self.store = Store(path)
        self.graph = Graph(self.store.modules())
        self.tools = Tools(self.store, self.graph, replay(self.store))
        self.agent = StandIn(self.tools)


def github_files(repo: str, pr: int) -> list[str]:
    """Changed files of a pull request from the REST API (webhook payloads do not carry them)."""
    import httpx

    token = os.environ["GITHUB_TOKEN"]
    out: list[str] = []
    with httpx.Client(
        base_url="https://api.github.com",
        timeout=20,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    ) as c:
        for page in range(1, 31):  # the API returns at most 3,000 files
            batch = c.get(f"/repos/{repo}/pulls/{pr}/files", params={"per_page": 100, "page": page}).raise_for_status()
            names = [f["filename"] for f in batch.json()]
            out += names
            if len(names) < 100:
                break
    return out


def create_app(
    secret: str | None = None,
    worlds: dict[str, Path] | None = None,
    reports: Path | None = None,
    files_of: Callable[[str, int], list[str]] = github_files,
) -> FastAPI:
    secret = secret if secret is not None else os.environ.get("DEVPROD_WEBHOOK_SECRET")
    if not secret:
        raise RuntimeError("DEVPROD_WEBHOOK_SECRET is not set; refusing to accept unsigned webhooks")
    paths = worlds or {p: settings.observed(p) for p in settings.POLICIES}
    reports = reports or settings.REPORTS
    loaded: dict[str, World] = {}
    app = FastAPI(title="devprod control plane", version="1.0.0")

    def world(name: str) -> World:
        if name not in paths:
            raise HTTPException(404, f"no world {name!r}; one of {sorted(paths)}")
        if not Path(paths[name]).exists():
            raise HTTPException(503, f"{name} has not been built yet: run `devprod run`")
        if name not in loaded:
            loaded[name] = World(paths[name])
        return loaded[name]

    deliveries = sqlite3.connect(":memory:", check_same_thread=False)
    deliveries.execute("create table seen (id text primary key, event text, action text)")

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "worlds": {k: Path(v).exists() for k, v in paths.items()}}

    @app.post("/webhooks/github")
    async def github(
        request: Request,
        x_hub_signature_256: str | None = Header(None),
        x_github_event: str | None = Header(None),
        x_github_delivery: str | None = Header(None),
        world_name: str = "control-plane",
    ) -> dict[str, Any]:
        body = await request.body()
        if not signature_ok(secret, body, x_hub_signature_256):
            raise HTTPException(401, "bad signature")
        if not x_github_delivery:
            raise HTTPException(400, "missing X-GitHub-Delivery")
        event = json.loads(body)
        try:
            deliveries.execute(
                "insert into seen values (?, ?, ?)", (x_github_delivery, x_github_event, event.get("action"))
            )
        except sqlite3.IntegrityError:
            return {"duplicate": True, "delivery": x_github_delivery}
        try:
            return handle(x_github_event or "", event, world_name)
        except Exception:
            deliveries.execute("delete from seen where id=?", (x_github_delivery,))  # let a redelivery retry
            raise

    def handle(kind: str, event: dict[str, Any], world_name: str) -> dict[str, Any]:
        x_github_event = kind
        if x_github_event == "ping":
            return {"pong": True}
        w = world(world_name)
        if x_github_event == "workflow_run" and event.get("action") == "completed":
            run = event["workflow_run"]
            if run.get("conclusion") != "failure":
                return {"ignored": f"conclusion {run.get('conclusion')}"}
            run_id = int(run["id"])
            known = w.store.rows("select pr, finished_ts from runs where run_id=?", (run_id,))
            if not known:  # the CI exporter has not mirrored this run's jobs and logs yet; GitHub will redeliver
                raise HTTPException(409, f"run {run_id} is not in the CI record yet")
            d = w.agent.diagnose(known[0]["pr"], run_id, now=known[0]["finished_ts"])
            return {"triaged": run_id, "pr": known[0]["pr"], **as_dict(d)}
        if x_github_event == "pull_request" and event.get("action") in ("opened", "synchronize", "reopened"):
            files = files_of(event["repository"]["full_name"], int(event["number"]))
            return {"pr": event["number"], "required": required_workflows(files, w.store.modules())}
        return {"ignored": f"{x_github_event}/{event.get('action')}"}

    @app.get("/api/worlds")
    def list_worlds() -> list[dict[str, Any]]:
        return [{"name": k, "built": Path(v).exists()} for k, v in paths.items()]

    @app.get("/api/worlds/{name}/ci")
    def ci_metrics(name: str) -> dict[str, Any]:
        return devprod.ci(world(name).store)

    @app.get("/api/worlds/{name}/delivery")
    def delivery_metrics(name: str) -> dict[str, Any]:
        return devprod.delivery(world(name).store)

    @app.get("/api/worlds/{name}/flaky")
    def flaky(name: str, limit: int = 50) -> dict[str, Any]:
        w = world(name)
        q = w.store.rows("select test_id, owner, since_ts, until_ts, reason, status from quarantine order by since_ts")
        return {"quarantine": q, "suspects": w.tools.flakes.summary()[:limit]}

    @app.get("/api/worlds/{name}/runs/{run_id}")
    def run(name: str, run_id: int) -> dict[str, Any]:
        w = world(name)
        r = w.store.rows("select * from runs where run_id=?", (run_id,))
        if not r:
            raise HTTPException(404, f"no run {run_id}")
        out: dict[str, Any] = {
            **r[0],
            "jobs": w.store.rows(
                "select job_id, module, shard, runner, conclusion, step_failed, minutes from jobs where run_id=?",
                (run_id,),
            ),
        }
        if r[0]["conclusion"] == "failure":
            out["diagnosis"] = as_dict(w.agent.diagnose(r[0]["pr"], run_id, r[0]["finished_ts"]))
        return out

    @app.get("/api/worlds/{name}/failures")
    def failures(name: str, limit: int = 40) -> list[dict[str, Any]]:
        w = world(name)
        return w.store.rows(
            "select r.run_id, r.pr, r.attempt, r.trigger, r.finished_ts, c.classification, c.body from runs r "
            "left join comments c using(run_id) where r.conclusion='failure' order by r.finished_ts desc limit ?",
            (limit,),
        )

    @app.get("/api/worlds/{name}/daily")
    def daily(name: str) -> list[dict[str, Any]]:
        """Per-day series for the dashboard charts."""
        s = world(name).store
        return s.rows(
            "select cast(r.started_ts / 1440 as int) day, count(*) runs, "
            "sum(r.conclusion='failure') failed, avg(r.finished_ts - r.started_ts) wall, "
            "(select runner_minutes from fleet_usage f where f.day = cast(r.started_ts / 1440 as int)) runner_minutes, "
            "(select peak_runners from fleet_usage f where f.day = cast(r.started_ts / 1440 as int)) peak_runners "
            "from runs r where r.trigger='push' group by 1 order by 1"
        )

    @app.get("/api/report")
    def report() -> dict[str, Any]:
        path = reports / "results.json"
        if not path.exists():
            raise HTTPException(503, "no report yet: run `devprod run`")
        return json.loads(path.read_text())

    return app
