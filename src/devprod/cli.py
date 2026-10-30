"""devprod: the command line of the control plane, for engineers, CI and agents alike."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Developer-productivity control plane.")
dev = typer.Typer(no_args_is_help=True, help="The inner loop: test and invoke a module locally.")
app.add_typer(dev, name="dev")

WORLD = typer.Option("control-plane", "--world", help="Which observed store to read (baseline | control-plane)")


def _store(world: str):
    from . import settings
    from .store import Store

    path = settings.observed(world)
    if not path.exists():
        typer.echo(f"{world} has not been built: run `devprod run` first", err=True)
        raise typer.Exit(2)
    return Store(path)


@app.command()
def run(skip_sim: bool = typer.Option(False, help="Reuse the simulated worlds in build/")) -> None:
    """Simulate both worlds, evaluate, write reports/ and end with the release gate."""
    from . import pipeline

    results = pipeline.run(log=typer.echo, simulate_first=not skip_sim)
    _print_gate(results["gate"])


@app.command()
def gate() -> None:
    """Re-check the release gate against reports/results.json."""
    from . import pipeline, settings

    results = json.loads((settings.REPORTS / "results.json").read_text())
    _print_gate(pipeline.gate(results))


def _print_gate(checks: list[dict]) -> None:
    for c in checks:
        typer.echo(f"{'pass' if c['passed'] else 'FAIL'}  {c['name']}: {c['value']} {c['op']} {c['bound']}")
    failed = [c for c in checks if not c["passed"]]
    typer.echo(f"{len(checks) - len(failed)}/{len(checks)} checks pass")
    if failed:
        raise typer.Exit(1)


@app.command()
def triage(
    run_id: int = typer.Option(..., "--run"),
    pr: int = typer.Option(0, "--pr"),
    world: str = WORLD,
    agent: str = typer.Option("", help="standin | claude (default: DEVPROD_AGENT or config)"),
) -> None:
    """Diagnose a failed run and print the comment the agent would post."""
    from . import settings
    from .agent.tools import Tools
    from .agent.triage import StandIn, as_dict
    from .ci.graph import Graph
    from .flaky.detector import replay

    store = _store(world)
    tools = Tools(store, Graph(store.modules()), replay(store))
    r = store.rows("select pr, finished_ts from runs where run_id=?", (run_id,))
    if not r:
        typer.echo(f"no run {run_id}", err=True)
        raise typer.Exit(2)
    pr = pr or r[0]["pr"]
    kind = agent or os.environ.get("DEVPROD_AGENT") or settings.load("agent")["provider"]
    if kind == "claude":
        from .agent import claude

        d, usage = asyncio.run(claude.diagnose(tools, pr, run_id, r[0]["finished_ts"]))
        typer.echo(json.dumps({**as_dict(d), "usage": usage}, indent=2))
    else:
        d = StandIn(tools).diagnose(pr, run_id, r[0]["finished_ts"])
        typer.echo(d.body())


@app.command("what-runs")
def what_runs(files: list[str], world: str = WORLD) -> None:
    """Which module workflows a change to these files starts."""
    from .ci.workflows import required_workflows

    typer.echo("\n".join(required_workflows(files, _store(world).modules())) or "(none)")


@app.command("plan-shards")
def plan_shards(module: str, target_minutes: float = 6.0, max: int = 8, world: str = WORLD) -> None:
    """The shard indexes for a module's test matrix, as JSON (used by the generated workflows)."""
    from .ci.sharding import shard

    store = _store(world)
    d = {r["test_id"]: r["seconds"] for r in store.rows("select test_id, seconds from tests where module=?", (module,))}
    typer.echo(json.dumps(list(range(len(shard(d, target_minutes, max))))))


@app.command("shard-tests")
def shard_tests(
    module: str,
    shard_index: int = typer.Option(..., "--shard"),
    target_minutes: float = 6.0,
    max: int = 8,
    world: str = WORLD,
) -> None:
    """The tests of one shard, one per line, balanced by recorded duration."""
    from .ci.sharding import shard

    store = _store(world)
    d = {r["test_id"]: r["seconds"] for r in store.rows("select test_id, seconds from tests where module=?", (module,))}
    shards = shard(d, target_minutes, max)
    if not 0 <= shard_index < len(shards):
        typer.echo(f"shard {shard_index} out of range 0..{len(shards) - 1}", err=True)
        raise typer.Exit(2)
    typer.echo("\n".join(shards[shard_index]))


@app.command()
def workflows(
    out: Path = typer.Option(Path("generated"), help="Where to write .github/"),
    world: str = WORLD,
    check: bool = typer.Option(False, help="Fail if the files on disk differ (for CI)"),
) -> None:
    """Generate one GitHub Actions workflow per module, ci-gate and CODEOWNERS."""
    from .ci.workflows import all_files

    files = all_files(_store(world).modules())
    stale = [p for p, t in files.items() if not (out / p).exists() or (out / p).read_text() != t]
    if check:
        if stale:
            typer.echo("out of date: " + ", ".join(stale), err=True)
            raise typer.Exit(1)
        typer.echo(f"{len(files)} files up to date")
        return
    for p, t in files.items():
        (out / p).parent.mkdir(parents=True, exist_ok=True)
        (out / p).write_text(t)
    typer.echo(f"wrote {len(files)} files under {out} ({len(stale)} changed)")


@app.command("gate-pr")
def gate_pr(
    base: str = typer.Option(...),
    head: str = typer.Option(...),
    world: str = WORLD,
    repo: str = typer.Option("", envvar="GITHUB_REPOSITORY"),
    timeout_minutes: float = 80,
    dry_run: bool = typer.Option(False, help="Print what would be waited for and stop"),
) -> None:
    """The one required check: wait for the module workflows this change starts, fail if any fails."""
    from .ci.workflows import required_workflows

    changed = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}"], capture_output=True, text=True, check=True
    ).stdout.split()
    need = required_workflows(changed, _store(world).modules())
    typer.echo(f"{len(changed)} files changed; waiting for: {', '.join(need) or 'nothing'}")
    if dry_run or not need:
        return
    import httpx

    h = {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"}
    deadline = time.time() + timeout_minutes * 60
    with httpx.Client(base_url="https://api.github.com", headers=h, timeout=20) as c:
        while time.time() < deadline:
            runs = c.get(f"/repos/{repo}/actions/runs", params={"head_sha": head, "per_page": 100}).json()
            latest = {}
            for r in sorted(runs.get("workflow_runs", []), key=lambda r: r["run_attempt"]):
                latest[r["name"]] = r
            failed = [
                n for n in need if n in latest and latest[n]["conclusion"] in ("failure", "cancelled", "timed_out")
            ]
            if failed:
                typer.echo(f"failed: {', '.join(failed)}", err=True)
                raise typer.Exit(1)
            if all(n in latest and latest[n]["conclusion"] == "success" for n in need):
                typer.echo("all required workflows passed")
                return
            time.sleep(20)
    typer.echo("timed out waiting for module workflows", err=True)
    raise typer.Exit(1)


@app.command()
def new(
    name: str,
    language: str = typer.Option("python"),
    deps: list[str] = typer.Option([], "--dep"),
    owner: str = typer.Option("platform"),
    root: Path = typer.Option(Path(".")),
    world: str = WORLD,
    dry_run: bool = typer.Option(False),
) -> None:
    """Scaffold a new module on the paved road (handler, tests, SAM template, workflow, owners)."""
    from .paved import scaffold

    try:
        files = scaffold.render(name, language, deps, _store(world).modules(), owner)
        if dry_run:
            typer.echo("\n".join(sorted(files)))
            return
        written = scaffold.write(root, files)
    except scaffold.ScaffoldError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from e
    typer.echo("\n".join(written))
    typer.echo(f"next: devprod dev test {name} --root {root}")


@dev.command("invoke")
def dev_invoke(module_dir: Path, event: Path) -> None:
    """Run a module's Lambda handler on an event file, locally, in seconds."""
    from .paved.local import invoke

    out = invoke(module_dir, event)
    typer.echo(json.dumps(out, indent=2))
    if not out["ok"]:
        raise typer.Exit(1)


@dev.command("test")
def dev_test(name: str, root: Path = typer.Option(Path("."))) -> None:
    """Run one module's tests the way its CI job does."""
    for base in (root / "services" / name, root / "frontend" / name, root / "libs" / name):
        if (base / "pyproject.toml").exists():
            env = {**os.environ, "PYTHONPATH": str(base / "src")}
            raise typer.Exit(subprocess.call([sys.executable, "-m", "pytest", "-q", str(base / "tests")], env=env))
        if (base / "package.json").exists():
            raise typer.Exit(subprocess.call(["npx", "vitest", "run"], cwd=base))
    typer.echo(f"no module {name} under {root}", err=True)
    raise typer.Exit(2)


@app.command("export-web")
def export_web() -> None:
    """Write the dashboard's data to web/public/data (from the reports and both worlds)."""
    from . import export

    typer.echo("wrote " + ", ".join(export.run()))


@app.command()
def heldout(name: str, world: str = WORLD) -> None:
    """Score the triage agent on a held-out set (its first run is recorded once and never rewritten)."""
    from . import heldout as h

    out = h.run(name, _store(world).modules())
    typer.echo(f"{name} ({out['status']}): {out['cases']} cases, kind right {out['kind_accuracy']:.0%}, "
               f"next step right {out['next_step_accuracy']:.0%}" + (" — first run recorded" if out.get("first_run")
                                                                     else ""))
    for r in out["results"]:
        if not r["kind_ok"]:
            typer.echo(f"  {r['id']}: expected {r['expected']}, got {r['got']} ({r['why']})")


@app.command("serve-mcp")
def serve_mcp(
    world: str = WORLD, http: bool = typer.Option(False, help="Streamable HTTP instead of stdio"), port: int = 8766
) -> None:
    """The platform as an MCP server for Claude Code, OpenCode or any MCP client."""
    from . import settings
    from .service.mcp_server import build

    _store(world)
    server = build(settings.observed(world))
    if http:
        server.run(transport="streamable-http", port=port)
    else:
        server.run()


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    """The webhook receiver and the dashboard API."""
    import uvicorn

    from .service.app import create_app

    uvicorn.run(create_app(), host=host, port=port)


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
