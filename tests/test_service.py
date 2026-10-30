"""The webhook receiver and the dashboard API."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from devprod.service.app import create_app, signature_ok
from tests.conftest import add_failed_run, add_pr

SECRET = "it-is-a-secret"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def client(store, tmp_path):
    add_pr(store, 1, ["schemas"])
    add_failed_run(store, 7, 1, "api", "FAILED services/api/tests/test_x.py::test_3 - AssertionError: assert 1 == 2")
    store.commit()
    app = create_app(
        SECRET,
        {"control-plane": store.path, "missing": tmp_path / "none.sqlite"},
        tmp_path,
        files_of=lambda repo, pr: ["libs/core/a.py", "docs/x.md"],
    )
    return TestClient(app)


def post(client, event, payload, delivery, secret=SECRET):
    body = json.dumps(payload).encode()
    return client.post(
        "/webhooks/github",
        content=body,
        headers={"X-Hub-Signature-256": sign(body, secret), "X-GitHub-Event": event, "X-GitHub-Delivery": delivery},
    )


def test_refuses_to_start_without_a_secret(monkeypatch):
    monkeypatch.delenv("DEVPROD_WEBHOOK_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        create_app()


def test_signature_check():
    body = b'{"a": 1}'
    assert signature_ok(SECRET, body, sign(body))
    assert not signature_ok(SECRET, body, sign(body, "other"))
    assert not signature_ok(SECRET, body + b" ", sign(body))
    assert not signature_ok(SECRET, body, None) and not signature_ok(SECRET, body, "sha1=abc")


def test_rejects_bad_signatures_and_missing_delivery(client):
    assert post(client, "ping", {}, "d1", secret="nope").status_code == 401
    body = b"{}"
    r = client.post("/webhooks/github", content=body, headers={"X-Hub-Signature-256": sign(body)})
    assert r.status_code == 400


def test_each_delivery_acts_once(client):
    assert post(client, "ping", {"zen": "x"}, "d1").json() == {"pong": True}
    assert post(client, "ping", {"zen": "x"}, "d1").json()["duplicate"] is True


def test_failed_run_is_triaged(client):
    r = post(client, "workflow_run", {"action": "completed", "workflow_run": {"id": 7, "conclusion": "failure"}}, "d2")
    assert r.status_code == 200 and r.json()["kind"] == "regression" and r.json()["pr"] == 1
    ok = post(client, "workflow_run", {"action": "completed", "workflow_run": {"id": 8, "conclusion": "success"}}, "d3")
    assert "ignored" in ok.json()


def test_unknown_run_is_retryable(client):
    payload = {"action": "completed", "workflow_run": {"id": 999, "conclusion": "failure"}}
    assert post(client, "workflow_run", payload, "d4").status_code == 409
    assert post(client, "workflow_run", payload, "d4").status_code == 409  # not marked as seen


def test_pull_request_gets_the_workflows_it_needs(client):
    r = post(client, "pull_request", {"action": "opened", "number": 3, "repository": {"full_name": "o/r"}}, "d5")
    assert r.json() == {"pr": 3, "required": ["ci / api", "ci / billing", "ci / core", "ci / schemas"]}


def test_api(client):
    assert client.get("/healthz").json()["worlds"] == {"control-plane": True, "missing": False}
    assert client.get("/api/worlds/missing/ci").status_code == 503
    assert client.get("/api/worlds/nope/ci").status_code == 404
    ci = client.get("/api/worlds/control-plane/ci").json()
    assert ci["prs"] == 1 and ci["runs"] == 1
    run = client.get("/api/worlds/control-plane/runs/7").json()
    assert run["diagnosis"]["kind"] == "regression" and run["jobs"][0]["module"] == "api"
    assert client.get("/api/worlds/control-plane/runs/70").status_code == 404
    assert client.get("/api/worlds/control-plane/failures").json()[0]["run_id"] == 7
    assert client.get("/api/worlds/control-plane/flaky").json() == {"quarantine": [], "suspects": []}
    assert client.get("/api/report").status_code == 503
