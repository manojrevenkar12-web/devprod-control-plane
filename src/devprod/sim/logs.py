"""What a failing job's log looks like: pytest and Jest output, pip and npm install errors, runner
deaths. Flaky failures and regressions are written so a log alone does not tell them apart: a slow
regression times out like a flaky test, and an order-dependent flaky test fails an assertion."""

from __future__ import annotations

import numpy as np

from .repo import Module, Test

INFRA = {
    "oom": "##[error]The runner has received a shutdown signal. ... Process completed with exit code 137.\nKilled",
    "disk": "OSError: [Errno 28] No space left on device: '/home/runner/_work/_temp'",
    "shutdown": "##[error]The runner has received a shutdown signal. This can happen when the runner service is stopped.",
    "network": "fatal: unable to access 'https://github.com/org/monorepo/': Could not resolve host: api.github.com",
}
DEPENDENCY = {
    "python": "ERROR: Could not find a version that satisfies the requirement {pkg}=={ver} (from versions: none)\n"
    "ERROR: No matching distribution found for {pkg}=={ver}",
    "typescript": "npm ERR! code ETARGET\nnpm ERR! notarget No matching version found for {pkg}@{ver}.",
}
PKGS = {"python": ["pydantic-core", "httpx", "boto3", "orjson"], "typescript": ["@tanstack/react-query", "vite", "zod"]}


def header(m: Module, shard: int, runner: str) -> str:
    return (
        f"Run {m.language} tests for {m.name} (shard {shard})\nRunner: {runner}\n"
        f"##[group]Restore cache\nCache restored from key: {m.name}-deps\n##[endgroup]\n"
    )


def _pytest_fail(t: Test, kind: str, r: np.random.Generator) -> tuple[str, str]:
    name = t.test_id.split("::")[1]
    if kind == "timeout":
        detail = f"Failed: Timeout >{int(r.integers(5, 30))}.0s"
        body = f"E       {detail}\n{t.file}:{int(r.integers(20, 300))}: Timeout"
    elif kind == "ordering":
        a, b = sorted(r.integers(1, 9, size=2).tolist())
        detail = f"AssertionError: assert [{a}, {b}] == [{b}, {a}]"
        body = f"E       {detail}\nE         At index 0 diff: {a} != {b}"
    elif kind == "network":
        detail = "httpx.ConnectError: [Errno 111] Connection refused"
        body = f"E       {detail}"
    else:  # regression: a value changed
        exp, got = int(r.integers(1, 500)), int(r.integers(1, 500))
        detail = f"AssertionError: assert {got} == {exp}"
        body = f"E       {detail}\nE        +  where {got} = result.value"
    return (
        f"_____ {name} _____\n\n    def {name}(client):\n>       assert result.value == expected\n{body}\n",
        f"FAILED {t.file}::{name} - {detail}",
    )


def _jest_fail(t: Test, kind: str, r: np.random.Generator) -> tuple[str, str]:
    name = t.test_id.split("::")[1].replace("_", " ")
    if kind == "timeout":
        detail = f'thrown: "Exceeded timeout of {int(r.integers(5, 30)) * 1000} ms for a test."'
    elif kind == "network":
        detail = "TypeError: fetch failed"
    else:
        detail = f"expect(received).toBe(expected)\n    Expected: {int(r.integers(1, 99))}\n    Received: {int(r.integers(1, 99))}"
    return f"  ● {name}\n\n    {detail}\n", f"    ✕ {name} ({int(r.integers(5, 900))} ms)"


def tests_failed(
    m: Module, shard: int, runner: str, failures: list[tuple[Test, str]], passed: int, r: np.random.Generator
) -> str:
    out = [header(m, shard, runner)]
    if m.language == "python":
        out.append(
            f"============================= test session starts ==============================\n"
            f"collected {passed + len(failures)} items\n"
        )
        bodies, summary = zip(*[_pytest_fail(t, k, r) for t, k in failures], strict=True)
        out += ["=================================== FAILURES ===================================\n", *bodies]
        out.append("=========================== short test summary info ============================")
        out += list(summary)
        out.append(f"=========== {len(failures)} failed, {passed} passed in {r.uniform(20, 400):.2f}s ===========")
    else:
        files = sorted({t.file for t, _ in failures})
        for f in files:
            out.append(f"FAIL {f}")
            for t, k in failures:
                if t.file == f:
                    body, line = _jest_fail(t, k, r)
                    out.append(line)
                    out.append(body)
        out.append(f"Tests:       {len(failures)} failed, {passed} passed, {passed + len(failures)} total")
    out.append("##[error]Process completed with exit code 1.")
    return "\n".join(out)


def infra(m: Module, shard: int, runner: str, kind: str) -> str:
    return header(m, shard, runner) + "Running tests...\n" + INFRA[kind]


def dependency(m: Module, shard: int, runner: str, r: np.random.Generator) -> str:
    pkg = PKGS[m.language][int(r.integers(len(PKGS[m.language])))]
    ver = f"{int(r.integers(1, 9))}.{int(r.integers(0, 20))}.{int(r.integers(0, 9))}"
    return f"Run {m.language} install for {m.name}\n" + DEPENDENCY[m.language].format(pkg=pkg, ver=ver)
