# 4. The triage agent proposes; a guard, not the prompt, limits what it does

**Status:** accepted

**Context.** A CI triage agent that can act is useful. It can rerun a job that died, or wait out a
registry outage. One that can quarantine, revert or push fixes on its own is a risk. Prompts are
not controls.

**Decision.**
- The agent gets the platform's tools as an in-process MCP server and no built-in tools at all.
- A PreToolUse hook allows reads, one rerun per failure, comments and quarantine proposals. It
  denies quarantine on main, fix PRs and reverts, with the reason.
- A suspected regression is confirmed by one rerun before the author is told.
- The answer must match a JSON schema. Spend per failure is capped.
- CI and the simulation run a rule-based stand-in on the same tools.

**Consequences.** Authors are rarely sent chasing noise: regression precision is 0.92 in the
simulation. The stand-in was first written against the simulator's own logs. On the first blind
held-out set it got 50% right: real pytest-xdist, Jest and Vitest output, and install failures caused
by the change's own manifest, were not recognised. That set became a regression set. The fixes were
generalised, and a second blind set measures the result.
