"""The platform decides from what GitHub, the CI, AWS and the cloud bill record. It never sees the
simulated organisation's truth: which tests are really flaky, which changes really break things."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).parents[1] / "src" / "devprod"
SHIPPING = ["agent", "ci", "flaky", "deploy", "metrics", "paved", "service", "store.py"]
FORBIDDEN_MODULES = ("devprod.sim", "devprod.evaluation", "devprod.pipeline", "devprod.report")


def files() -> list[Path]:
    out = []
    for name in SHIPPING:
        p = SRC / name
        out += sorted(p.rglob("*.py")) if p.is_dir() else [p]
    return out


def resolve(node: ast.ImportFrom, path: Path) -> str:
    if not node.level:
        return node.module or ""
    pkg = path.relative_to(SRC.parent).with_suffix("").parts[: -node.level]
    return ".".join([*pkg, *([node.module] if node.module else [])])


@pytest.mark.parametrize("path", files(), ids=lambda p: str(p.relative_to(SRC)))
def test_shipping_code_never_reaches_the_truth(path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = resolve(node, path)
            names = [base, *(f"{base}.{a.name}" for a in node.names)]
        else:
            names = []
        for n in names:
            assert not n.startswith(FORBIDDEN_MODULES), f"{path.name} imports {n}"
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "truth":
                raise AssertionError(f"{path.name} calls settings.truth()")
            if (
                isinstance(f, ast.Attribute)
                and f.attr == "load"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "org"
            ):
                raise AssertionError(f"{path.name} reads config/org.yaml")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "/truth" in node.value:
            raise AssertionError(f"{path.name} names a truth path")


def test_the_scan_covers_the_shipping_code():
    assert len(files()) >= 20


def test_relative_imports_are_resolved():
    node = ast.parse("from ..sim import world").body[0]
    assert resolve(node, SRC / "agent" / "x.py") == "devprod.sim"
    node = ast.parse("from .tools import Tools").body[0]
    assert resolve(node, SRC / "agent" / "x.py") == "devprod.agent.tools"
