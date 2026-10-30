"""The platform as an MCP server for engineers' own agents (Claude Code, OpenCode, any MCP client).

`devprod serve-mcp` serves it over stdio for a local agent, or over streamable HTTP for a shared one.
Every tool reads; none changes CI or a deployment. An agent can ask why a run failed (the same
diagnosis the triage agent posts), what a change will run, which tests are quarantined and who owns
them, how CI and delivery are doing, the workflow a module gets, and what scaffolding a new module
would add — the paved road, reachable from the terminal an engineer already works in.

Register with Claude Code:  claude mcp add devprod -- uv run devprod serve-mcp
OpenCode (opencode.json):   {"mcp": {"devprod": {"type": "local", "command": ["uv", "run", "devprod", "serve-mcp"]}}}"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..agent.tools import Tools
from ..agent.triage import StandIn, as_dict
from ..ci.graph import Graph
from ..ci.workflows import workflow_for
from ..flaky.detector import replay
from ..metrics import devprod
from ..paved import scaffold
from ..store import Store

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def build(store_path: Path | str) -> MCPServer:
    store = Store(store_path)
    graph = Graph(store.modules())
    flakes = replay(store)
    tools = Tools(store, graph, flakes)
    agent = StandIn(tools)
    mcp = MCPServer(
        name="devprod",
        instructions=(
            "The platform team's developer-productivity control plane: CI failures and their diagnosis, flaky "
            "tests, what a change runs, CI and delivery metrics, workflows and module scaffolding. Read-only."
        ),
    )

    @mcp.tool(annotations=READ)
    def explain_run(run_id: int) -> dict[str, Any]:
        """Why a CI run failed (infra, dependency, flaky or regression), with evidence and the next step."""
        r = store.rows("select pr, finished_ts, conclusion from runs where run_id=?", (run_id,))
        if not r:
            return {"error": f"no run {run_id}"}
        if r[0]["conclusion"] != "failure":
            return {"run_id": run_id, "conclusion": r[0]["conclusion"]}
        d = agent.diagnose(r[0]["pr"], run_id, r[0]["finished_ts"])
        return {"run_id": run_id, "pr": r[0]["pr"], **as_dict(d)}

    @mcp.tool(annotations=READ)
    def pr_failures(pr: int) -> list[dict[str, Any]]:
        """Failed runs of a pull request and what the triage agent said about each."""
        runs = store.rows(
            "select run_id, sha, attempt, trigger, finished_ts from runs where pr=? and "
            "conclusion='failure' order by run_id",
            (pr,),
        )
        said = {
            c["run_id"]: c for c in store.rows("select run_id, classification, body from comments where pr=?", (pr,))
        }
        return [
            {
                **r,
                "triage": said.get(r["run_id"], {}).get("classification"),
                "comment": said.get(r["run_id"], {}).get("body"),
            }
            for r in runs
        ]

    @mcp.tool(annotations=READ)
    def what_will_run(files: list[str]) -> dict[str, Any]:
        """The modules a change to these files runs CI for: the touched modules and everything depending on them."""
        touched = sorted({m for f in files if (m := graph.module_of(f))})
        return {
            "touched": touched,
            "runs": sorted(graph.affected(touched)),
            "unmatched_files": [f for f in files if graph.module_of(f) is None],
        }

    @mcp.tool(annotations=READ)
    def test_history(test_id: str) -> dict[str, Any]:
        """Runs, failures, flips (failed then passed on the same commit), P(flaky) and quarantine status."""
        return tools.test_history(test_id)

    @mcp.tool(annotations=READ)
    def quarantined_tests() -> list[dict[str, Any]]:
        """Tests that run but do not block merges, the owning team and the deadline to fix them."""
        return store.rows("select test_id, owner, since_ts, until_ts, reason, status from quarantine order by since_ts")

    @mcp.tool(annotations=READ)
    def ci_metrics() -> dict[str, Any]:
        """PR lead time, CI wall time, push-to-green, queue, flake signal, runner minutes and PRs per engineer-day."""
        return devprod.ci(store)

    @mcp.tool(annotations=READ)
    def delivery_metrics() -> dict[str, Any]:
        """Deploy frequency, change failure rate, lead time for changes and time to restore (DORA)."""
        return devprod.delivery(store)

    @mcp.tool(annotations=READ)
    def workflow(module: str) -> str:
        """The GitHub Actions workflow the platform generates for a module (path filters include its dependencies)."""
        return workflow_for(module, store.modules())

    @mcp.tool(annotations=READ)
    def plan_new_module(name: str, language: str = "python", deps: list[str] | None = None) -> dict[str, Any]:
        """The files `devprod new` would create for a new Lambda module, without writing them."""
        files = scaffold.render(name, language, deps or [], store.modules())
        return {"files": sorted(files), "workflow": files.get(f".github/workflows/{name}.yml", "")}

    return mcp


def dumps(x: Any) -> str:
    return json.dumps(x, default=str)
