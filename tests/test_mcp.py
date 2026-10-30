"""The engineers' MCP server, on the small store."""

from __future__ import annotations

import json

from mcp.client.client import Client

from devprod.service.mcp_server import build
from tests.conftest import add_failed_run, add_pr


def payload(r):
    if r.structured_content and "result" in r.structured_content:
        return r.structured_content["result"]
    texts = [json.loads(c.text) if c.text[:1] in "[{" else c.text for c in r.content]
    return texts[0] if len(texts) == 1 else texts


async def test_every_tool_answers_and_is_read_only(store):
    add_pr(store, 1, ["schemas"])
    add_failed_run(store, 7, 1, "api", "FAILED services/api/tests/test_x.py::test_3 - AssertionError")
    store.insert(
        "comments",
        {"pr": 1, "ts": 1, "author": "triage-agent", "body": "b", "classification": "regression", "run_id": 7},
    )
    store.commit()
    async with Client(build(store.path)) as c:
        tools = (await c.list_tools()).tools
        assert all(t.annotations.read_only_hint for t in tools)
        assert payload(await c.call_tool("explain_run", {"run_id": 7}))["kind"] == "regression"
        assert payload(await c.call_tool("explain_run", {"run_id": 70}))["error"]
        fails = payload(await c.call_tool("pr_failures", {"pr": 1}))
        fails = fails if isinstance(fails, list) else [fails]
        assert fails[0]["triage"] == "regression"
        runs = payload(await c.call_tool("what_will_run", {"files": ["libs/schemas/a.py", "README.md"]}))
        assert runs == {"touched": ["schemas"], "runs": ["api", "schemas"], "unmatched_files": ["README.md"]}
        assert payload(await c.call_tool("test_history", {"test_id": "api::test_3"}))["quarantined"] is False
        assert payload(await c.call_tool("ci_metrics", {}))["runs"] == 1
        assert "change_failure_rate" in payload(await c.call_tool("delivery_metrics", {}))
        assert "ci / api" in payload(await c.call_tool("workflow", {"module": "api"}))
        plan = payload(await c.call_tool("plan_new_module", {"name": "svc-new", "deps": ["core"]}))
        assert "services/svc-new/template.yaml" in plan["files"]
