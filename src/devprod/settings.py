"""Where things live, and the settings. Platform settings are in config/, never in code.

`config/org.yaml` describes the simulated engineering organisation, including which tests are really
flaky and which changes really break things. Only `devprod.sim` and `devprod.evaluation` may read it,
or anything under build/<world>/truth (`tests/test_isolation.py` checks)."""

from __future__ import annotations

import os
from functools import cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config"
BUILD = Path(os.environ.get("DEVPROD_BUILD", ROOT / "build"))
REPORTS = Path(os.environ.get("DEVPROD_REPORTS", ROOT / "reports"))
HELDOUT = ROOT / "heldout"
POLICIES = ("baseline", "control-plane")


@cache
def load(name: str) -> dict[str, Any]:
    data = yaml.safe_load((CONFIG / f"{name}.yaml").read_text())
    override = os.environ.get("DEVPROD_SETTINGS_OVERRIDE")
    if override:
        extra = yaml.safe_load(Path(override).read_text()) or {}
        if name in extra:
            data = _merge(data, extra[name])
    return data


def _merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def world(policy: str) -> Path:
    """Observed data of a world run under a policy (what GitHub, the CI and AWS would hold)."""
    return BUILD / policy


def observed(policy: str) -> Path:
    return world(policy) / "observed.sqlite"


def truth(policy: str) -> Path:
    return world(policy) / "truth"
