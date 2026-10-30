"""devprod: the command line of the control plane."""

from __future__ import annotations

import json
import sys

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Developer-productivity control plane.")


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


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
