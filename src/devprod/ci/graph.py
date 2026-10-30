"""The monorepo's module graph: which modules a change affects, including everything that depends
on a changed module (a change to `core` runs `core` and its dependents, not the whole repository)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any


class Graph:
    def __init__(self, modules: dict[str, dict[str, Any]]) -> None:
        self.modules = modules
        self.dependents: dict[str, set[str]] = defaultdict(set)
        for name, m in modules.items():
            for d in m["deps"]:
                self.dependents[d].add(name)

    def module_of(self, path: str) -> str | None:
        best = None
        for name, m in self.modules.items():
            if path.startswith(m["path"] + "/") and (best is None or len(m["path"]) > len(self.modules[best]["path"])):
                best = name
        return best

    def affected(self, changed: Iterable[str]) -> set[str]:
        out: set[str] = set()
        todo = [m for m in changed if m in self.modules]
        while todo:
            m = todo.pop()
            if m in out:
                continue
            out.add(m)
            todo.extend(self.dependents.get(m, ()))
        return out

    def affected_by_files(self, files: Iterable[str]) -> set[str]:
        return self.affected({m for f in files if (m := self.module_of(f))})

    def order(self) -> list[str]:
        """Modules in dependency order (dependencies first)."""
        seen: set[str] = set()
        out: list[str] = []

        def visit(m: str) -> None:
            if m in seen:
                return
            seen.add(m)
            for d in self.modules[m]["deps"]:
                visit(d)
            out.append(m)

        for m in sorted(self.modules):
            visit(m)
        return out
