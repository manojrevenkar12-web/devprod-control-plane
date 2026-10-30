"""The monorepo: modules (Python services and libraries, TypeScript apps and packages), their
dependencies and owners, and their tests, including which tests are really flaky (truth)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .. import settings

PY = [
    "core",
    "schemas",
    "decision-runtime",
    "rules-engine",
    "flow-editor-api",
    "connectors-bureau",
    "connectors-banking",
    "connectors-kyc",
    "agent-runtime",
    "risk-models",
    "decision-api",
    "audit-log",
    "data-sync",
    "notifications",
    "billing",
    "auth",
    "reporting",
    "feature-store",
]
TS = ["ui-kit", "web-app", "flow-editor", "admin-console", "docs-site", "analytics-ui", "sdk-js"]
TEAM_OF = {
    "core": "platform",
    "schemas": "platform",
    "decision-runtime": "decisions",
    "rules-engine": "decisions",
    "flow-editor-api": "decisions",
    "connectors-bureau": "connectors",
    "connectors-banking": "connectors",
    "connectors-kyc": "connectors",
    "agent-runtime": "decisions",
    "risk-models": "risk",
    "decision-api": "decisions",
    "audit-log": "platform",
    "data-sync": "data",
    "notifications": "platform",
    "billing": "data",
    "auth": "platform",
    "reporting": "data",
    "feature-store": "risk",
    "ui-kit": "frontend",
    "web-app": "frontend",
    "flow-editor": "frontend",
    "admin-console": "frontend",
    "docs-site": "frontend",
    "analytics-ui": "frontend",
    "sdk-js": "frontend",
}
VERBS = [
    "evaluates",
    "rejects",
    "routes",
    "retries",
    "serialises",
    "validates",
    "merges",
    "caches",
    "renders",
    "paginates",
    "authorises",
    "masks",
    "aggregates",
    "times_out",
    "migrates",
]
NOUNS = [
    "decision",
    "rule",
    "flow",
    "connector_call",
    "score",
    "audit_event",
    "webhook",
    "schema",
    "token",
    "report",
    "feature",
    "batch",
    "node",
    "policy",
    "payload",
]


def rng(*key: Any) -> np.random.Generator:
    h = hashlib.sha256("|".join(map(str, (settings.load("org")["seed"], *key))).encode()).digest()
    return np.random.default_rng(int.from_bytes(h[:8], "little"))


@dataclass
class Test:
    test_id: str
    module: str
    file: str
    seconds: float
    flake_rate: float = 0.0  # truth
    flake_kind: str | None = None  # truth


@dataclass
class Module:
    name: str
    language: str
    path: str
    deps: list[str]
    owner: str
    tests: list[Test] = field(default_factory=list)


def build() -> dict[str, Module]:
    cfg = settings.load("org")
    rc, tc = cfg["repo"], cfg["tests"]
    r = rng("repo")
    mods: dict[str, Module] = {}
    for name in PY[: rc["python_modules"]]:
        if name in rc["shared_python"]:
            deps = [d for d in rc["shared_python"] if rc["shared_python"].index(d) < rc["shared_python"].index(name)]
        else:
            deps = [d for d in rc["shared_python"] if r.random() < 0.8] or ["core"]
            others = [m for m in PY[: rc["python_modules"]] if m not in rc["shared_python"] and m < name]
            deps += [str(m) for m in r.choice(others, size=min(len(others), int(r.integers(0, 3))), replace=False)]
        mods[name] = Module(
            name,
            "python",
            f"services/{name}" if name not in rc["shared_python"] else f"libs/{name}",
            sorted(set(deps)),
            TEAM_OF[name],
        )
    for name in TS[: rc["ts_modules"]]:
        deps = [] if name in rc["shared_ts"] else ["ui-kit"] + (["sdk-js"] if name == "web-app" else [])
        mods[name] = Module(name, "typescript", f"frontend/{name}", deps, TEAM_OF[name])
    for m in mods.values():
        n = int(r.integers(*rc["tests_per_module"]))
        files = max(3, n // 25)
        for k in range(n):
            f = k % files
            verb, noun = VERBS[int(r.integers(len(VERBS)))], NOUNS[int(r.integers(len(NOUNS)))]
            name = f"test_{verb}_{noun}_{k}"
            sec = float(np.exp(r.normal(np.log(rc["test_seconds"]["median"]), rc["test_seconds"]["sd"])))
            if m.language == "typescript":
                sec *= 0.5
            path = (
                f"{m.path}/tests/test_{NOUNS[f % len(NOUNS)]}_{f}.py"
                if m.language == "python"
                else f"{m.path}/src/{NOUNS[f % len(NOUNS)].title().replace('_', '')}{f}.test.tsx"
            )
            t = Test(f"{m.name}::{name}", m.name, path, round(sec, 3))
            if r.random() < tc["flaky_share"]:
                t.flake_rate = float(r.uniform(*tc["flake_rate"]))
                kinds = tc["flaky_kinds"]
                t.flake_kind = str(r.choice(list(kinds), p=np.array(list(kinds.values())) / sum(kinds.values())))
            m.tests.append(t)
    return mods
