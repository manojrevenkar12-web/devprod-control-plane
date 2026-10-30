"""The inner loop on a laptop: invoke a module's Lambda handler with an event file, in a fresh
interpreter with only that module on the path, the way Lambda would load it (no deploy, no Docker).
`sam local invoke` remains available for the full container; this is the seconds-not-minutes path."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

RUNNER = """
import importlib, json, sys, time
sys.path.insert(0, sys.argv[1])
mod, fn = sys.argv[2].rsplit(".", 1)
class Context:
    aws_request_id = "local"
    function_name = sys.argv[4]
    def get_remaining_time_in_millis(self): return 10_000
t = time.perf_counter()
out = getattr(importlib.import_module(mod), fn)(json.load(open(sys.argv[3])), Context())
print(json.dumps({"result": out, "ms": round((time.perf_counter() - t) * 1000, 2)}))
"""


def handler_of(module_dir: Path) -> str:
    """The Handler of the module's function in template.yaml."""
    for line in (module_dir / "template.yaml").read_text().splitlines():
        if line.strip().startswith("Handler:"):
            return line.split(":", 1)[1].strip()
    raise ValueError(f"no Handler in {module_dir / 'template.yaml'}")


def invoke(module_dir: Path, event: Path, timeout: float = 30) -> dict[str, Any]:
    t = time.perf_counter()
    p = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            RUNNER,
            str(module_dir / "src"),
            handler_of(module_dir),
            str(event),
            module_dir.name,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if p.returncode != 0:
        return {
            "ok": False,
            "error": p.stderr.strip().splitlines()[-1] if p.stderr.strip() else "failed",
            "stderr": p.stderr[-2000:],
        }
    lines = p.stdout.strip().splitlines()
    out = json.loads(lines[-1])
    return {
        "ok": True,
        **out,
        "logs": [*lines[:-1], *p.stderr.splitlines()],
        "wall_ms": round((time.perf_counter() - t) * 1000, 1),
    }
