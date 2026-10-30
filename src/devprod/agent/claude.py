"""The triage agent on Claude, through the Claude Agent SDK.

The model gets the same tools as the stand-in, served in-process as an MCP server (`devprod`), and no
built-in tools at all: no shell, no file edits, no web. Two subagents keep the main context small: a
log reader (Haiku) that turns a failed job's log into failing tests and markers, and a history checker
(Haiku) that looks up each test's flip history and the pull request's reach. The main agent (Sonnet)
decides and must answer in the Diagnosis schema.

What the agent may do is decided here, not in the prompt: a PreToolUse hook allows only the tools in
`config/agent.yaml: may_do_alone` plus reads, at most one rerun per failure, and denies everything in
`needs_a_person` (quarantine on main, a fix PR, a revert) with the reason, so the model proposes and
a person decides. Spend is capped per failure by `max_budget_usd`.

Running this needs the Claude Code CLI and an API key (DEVPROD_AGENT=claude); CI and the simulation
use `StandIn`. The tests build the options, call the tools and the guard directly, and check the
schema round-trip without a model."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    AgentDefinition,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    SdkMcpTool,
    create_sdk_mcp_server,
    query,
    tool,
)
from mcp.types import ToolAnnotations

from .. import settings
from .tools import Tools, markers
from .triage import KINDS, Diagnosis

SERVER = "devprod"
READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
ACT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
READS = ("failed_jobs", "failed_tests", "test_history", "pr_changes", "recent_step_failures", "failure_markers")
NEXT_STEPS = ("rerun_failed_jobs", "rerun_to_confirm", "wait_and_rerun", "tell_author", "propose_quarantine")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "confidence", "tests", "evidence", "next_step"],
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "tests": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
        "evidence": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 6},
        "next_step": {"type": "string", "enum": list(NEXT_STEPS)},
    },
}

PROMPT = """You triage failed CI runs in a monorepo. Decide why run {run_id} of pull request #{pr} failed:
- infra: the runner or the network failed (disk full, killed, lost communication). Rerun the failed jobs.
- dependency: installing packages failed, usually for many pull requests at once. Wait and rerun.
- flaky: a test that flips on reruns of the same commit, or that this change cannot reach. Rerun.
- regression: the change broke a test it reaches. Confirm with one rerun first; only a repeated
  failure is a regression. Then tell the author which test and why.
Use the log-reader for logs and the history-checker for test history and the change's reach.
Evidence must quote what you saw (a log line, a flip count, a module the change reaches).
{confirmed}Answer only with the JSON object."""


def _text(data: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(data, default=str)}]}


@dataclass
class Guard:
    """The PreToolUse hook: reads are free, actions are limited, people decide the rest."""

    cfg: dict[str, Any] = field(default_factory=lambda: settings.load("agent"))
    reruns: int = 0
    denied: list[dict[str, Any]] = field(default_factory=list)

    def allowed_names(self) -> list[str]:
        return [f"mcp__{SERVER}__{n}" for n in (*READS, *self.cfg["may_do_alone"])]

    async def __call__(self, hook_input: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        name = str(hook_input.get("tool_name", ""))
        short = name.removeprefix(f"mcp__{SERVER}__")
        reason = None
        if not name.startswith(f"mcp__{SERVER}__") and name != "Task" and name != "Agent":
            reason = f"{name} is not available to the triage agent"
        elif short in self.cfg["needs_a_person"]:
            reason = f"{short} needs a person: propose it in the diagnosis instead"
        elif short == "rerun_failed_jobs":
            if self.reruns >= 1:
                reason = "this failure has already been rerun once"
            self.reruns += 1
        elif short not in READS and short not in self.cfg["may_do_alone"] and name not in ("Task", "Agent"):
            reason = f"{short} is not on the list of things the agent may do alone"
        if reason is None:
            return {}
        self.denied.append({"tool": name, "reason": reason})
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }


def build_tools(t: Tools, pr: int, now: float) -> list[SdkMcpTool[Any]]:
    @tool(
        "failed_jobs",
        "Failed jobs of a CI run, with the last 4,000 characters of each log.",
        {"run_id": int},
        annotations=READ,
    )
    async def failed_jobs(args: dict[str, Any]) -> dict[str, Any]:
        return _text(t.failed_jobs(int(args["run_id"])))

    @tool(
        "failed_tests",
        "Test ids that failed in a job log (pytest and jest formats).",
        {"log": str, "module": str},
        annotations=READ,
    )
    async def failed_tests(args: dict[str, Any]) -> dict[str, Any]:
        return _text(t.failed_tests(args["log"], args["module"]))

    @tool(
        "test_history",
        "A test's runs, failures, flips (failed then passed on the same commit) and P(flaky).",
        {"test_id": str},
        annotations=READ,
    )
    async def test_history(args: dict[str, Any]) -> dict[str, Any]:
        return _text(t.test_history(args["test_id"]))

    @tool(
        "pr_changes",
        "Modules and files a pull request changes, and every module that depends on them.",
        {"pr": int},
        annotations=READ,
    )
    async def pr_changes(args: dict[str, Any]) -> dict[str, Any]:
        return _text(t.pr_changes(int(args["pr"])))

    @tool(
        "recent_step_failures",
        "How many pull requests failed at a step (e.g. install) in the last two hours.",
        {"step": str},
        annotations=READ,
    )
    async def recent_step_failures(args: dict[str, Any]) -> dict[str, Any]:
        return _text({"pull_requests": t.recent_step_failures(args["step"], now - 120, now)})

    @tool(
        "failure_markers",
        "Log lines that mean a runner failure (infra) or a package registry failure.",
        {},
        annotations=READ,
    )
    async def failure_markers(args: dict[str, Any]) -> dict[str, Any]:
        return _text(markers())

    @tool(
        "rerun_failed_jobs",
        "Rerun only the failed jobs of a run. Allowed once per failure.",
        {"run_id": int, "reason": str},
        annotations=ACT,
    )
    async def rerun_failed_jobs(args: dict[str, Any]) -> dict[str, Any]:
        return _text(t.rerun_failed_jobs(int(args["run_id"]), args["reason"]))

    @tool(
        "comment",
        "Post the diagnosis on the pull request.",
        {"body": str, "classification": str, "run_id": int},
        annotations=ACT,
    )
    async def comment(args: dict[str, Any]) -> dict[str, Any]:
        t.comment(pr, now, args["body"], args["classification"], int(args["run_id"]))
        return _text({"posted": True})

    @tool(
        "propose_quarantine",
        "Propose quarantining a flaky test; the owning team decides.",
        {"test_id": str, "evidence": str},
        annotations=ACT,
    )
    async def propose_quarantine(args: dict[str, Any]) -> dict[str, Any]:
        a = {"action": "propose_quarantine", "test_id": args["test_id"], "evidence": args["evidence"]}
        t.actions.append(a)
        return _text(a)

    return [
        failed_jobs,
        failed_tests,
        test_history,
        pr_changes,
        recent_step_failures,
        failure_markers,
        rerun_failed_jobs,
        comment,
        propose_quarantine,
    ]


def subagents() -> dict[str, AgentDefinition]:
    reads = [f"mcp__{SERVER}__{n}" for n in READS]
    return {
        "log-reader": AgentDefinition(
            description="Reads failed job logs and returns failing test ids and any infra or dependency markers.",
            prompt="Call failed_jobs, then failed_tests on each log, and failure_markers. Return the failing tests "
            "per job and any marker lines verbatim. Do not interpret beyond that.",
            tools=[reads[0], reads[1], reads[5]],
            model="haiku",
            maxTurns=6,
        ),
        "history-checker": AgentDefinition(
            description="Looks up flip history for tests and which modules a pull request reaches.",
            prompt="For each test id given, call test_history; call pr_changes for the pull request and "
            "recent_step_failures for 'install'. Return the numbers, not a verdict.",
            tools=[reads[2], reads[3], reads[4]],
            model="haiku",
            maxTurns=8,
        ),
    }


def options(t: Tools, pr: int, now: float, guard: Guard | None = None) -> ClaudeAgentOptions:
    cfg = settings.load("agent")
    guard = guard or Guard(cfg)
    server = create_sdk_mcp_server(SERVER, "1.0.0", build_tools(t, pr, now))
    return ClaudeAgentOptions(
        tools=[],  # no built-in tools: no shell, files or web
        allowed_tools=[*guard.allowed_names(), "Task", "Agent"],  # the subagent tool (renamed Agent in newer CLIs)
        mcp_servers={SERVER: server},
        agents=subagents(),
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[guard])]},
        system_prompt="You are the CI triage agent of a platform team. Be brief and exact.",
        model=cfg["model"],
        fallback_model=cfg["fallback_model"],
        max_turns=cfg["max_turns"],
        max_budget_usd=cfg["max_budget_usd_per_failure"],
        output_format={"type": "json_schema", "schema": SCHEMA},
        permission_mode="dontAsk",
        setting_sources=[],  # ignore the machine's CLAUDE.md and settings: reproducible
    )


def prompt(pr: int, run_id: int, confirmed: bool | None) -> str:
    note = (
        ""
        if confirmed is None
        else (
            "This is the confirming rerun of a suspected regression and it failed again.\n"
            if confirmed
            else "The confirming rerun of a suspected regression passed.\n"
        )
    )
    return PROMPT.format(pr=pr, run_id=run_id, confirmed=note)


def parse(out: Any) -> Diagnosis:
    """The schema-checked answer as a Diagnosis; anything else is an infra guess a person will see."""
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except json.JSONDecodeError:
            out = None
    if not isinstance(out, dict) or out.get("kind") not in KINDS or out.get("next_step") not in NEXT_STEPS:
        return Diagnosis("infra", 0.0, evidence=["the agent gave no valid answer"], next_step="tell_author")
    return Diagnosis(
        out["kind"], float(out["confidence"]), list(out.get("tests", [])), list(out["evidence"]), out["next_step"]
    )


async def diagnose(
    t: Tools, pr: int, run_id: int, now: float, confirmed: bool | None = None
) -> tuple[Diagnosis, dict[str, Any]]:
    guard = Guard()
    result: ResultMessage | None = None
    async for msg in query(prompt=prompt(pr, run_id, confirmed), options=options(t, pr, now, guard)):
        if isinstance(msg, ResultMessage):
            result = msg
    d = parse(result.structured_output if result else None)
    usage = {
        "cost_usd": result.total_cost_usd if result else None,
        "turns": result.num_turns if result else 0,
        "denied": guard.denied,
    }
    return d, usage
