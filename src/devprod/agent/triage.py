"""The CI triage agent: why did this run fail, and what should happen next?

Four answers: **infra** (the runner died: rerun the job on a fresh runner), **dependency** (an install
step broke, usually for many pull requests at once: wait, rerun when the registry recovers, do not
bother the author), **flaky** (a known or likely flaky test on code the change cannot reach: rerun the
failed job, propose quarantine), **regression** (the change broke a test it can reach: tell the author
which test and why). A suspected regression is confirmed by one rerun of the failed job; only a
repeated failure is called a regression, so authors are not sent chasing noise.

`StandIn` decides with the tools and rules below and is what CI and the simulation run without a model
key. `devprod.agent.claude` runs the same tools through the Claude Agent SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import settings
from .tools import Tools, markers

KINDS = ("infra", "dependency", "flaky", "regression")


@dataclass
class Diagnosis:
    kind: str
    confidence: float
    tests: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    next_step: str = "comment"  # rerun_failed_jobs | rerun_to_confirm | wait_and_rerun | tell_author
    calls: int = 0

    def body(self) -> str:
        lines = [f"**CI triage: {self.kind}** (confidence {self.confidence:.0%})"]
        if self.tests:
            lines.append("Tests: " + ", ".join(f"`{t}`" for t in self.tests[:6]))
        lines += [f"- {e}" for e in self.evidence[:6]]
        lines.append(f"Next: {self.next_step.replace('_', ' ')}")
        return "\n".join(lines)


class StandIn:
    def __init__(self, tools: Tools) -> None:
        self.t = tools
        self.cfg = settings.load("agent")
        self.mk = markers()

    def diagnose(self, pr: int, run_id: int, now: float, confirmed: bool | None = None) -> Diagnosis:
        """`confirmed`: None on the first look; True/False after the confirming rerun failed/passed."""
        jobs = self.t.failed_jobs(run_id)
        calls = 1
        if not jobs:
            return Diagnosis(
                "infra", 0.3, evidence=["no failed job recorded"], next_step="rerun_failed_jobs", calls=calls
            )
        logs = "\n".join(j["log_tail"] for j in jobs)
        if any(m in logs for m in self.mk["infra"]):
            hit = next(m for m in self.mk["infra"] if m in logs)
            return Diagnosis(
                "infra",
                0.9,
                evidence=[f"runner failure in the log: '{hit}'"],
                next_step="rerun_failed_jobs",
                calls=calls,
            )
        if any(m in logs for m in self.mk["dependency"]):
            hit = next(m for m in self.mk["dependency"] if m in logs)
            others = self.t.recent_step_failures("install", now - 120, now)
            calls += 1
            return Diagnosis(
                "dependency",
                0.95 if others >= 3 else 0.7,
                evidence=[f"install failed: '{hit}'", f"{others} pull requests failed at install in 2 h"],
                next_step="wait_and_rerun",
                calls=calls,
            )
        changes = self.t.pr_changes(pr)
        reach = set(changes["affected"])
        calls += 1
        tests, suspects, flaky = [], [], []
        for j in jobs:
            for test in self.t.failed_tests(j["log_tail"], j["module"]):
                tests.append(test)
                h = self.t.test_history(test)
                calls += 1
                module = test.split("::")[0]
                if h["quarantined"] or h["p_flaky"] >= 0.5:
                    flaky.append((test, f"`{test}` flipped {h['flips']} times in {h['runs']} runs"))
                elif module in reach:
                    suspects.append((test, f"`{test}` is in `{module}`, which this change reaches"))
                else:
                    flaky.append((test, f"`{test}` is in `{module}`, which this change cannot reach"))
        if not tests:
            return Diagnosis(
                "infra",
                0.4,
                evidence=["the job failed without a failed test or a known marker"],
                next_step="rerun_failed_jobs",
                calls=calls,
            )
        if suspects:
            if confirmed is None and self.cfg["confirm_regression_by_rerun"]:
                return Diagnosis(
                    "regression",
                    0.6,
                    [t for t, _ in suspects],
                    [e for _, e in suspects],
                    next_step="rerun_to_confirm",
                    calls=calls,
                )
            if confirmed is False:
                return Diagnosis(
                    "flaky",
                    0.8,
                    [t for t, _ in suspects],
                    [e for _, e in suspects] + ["passed when rerun on the same commit"],
                    next_step="propose_quarantine",
                    calls=calls,
                )
            return Diagnosis(
                "regression",
                0.92,
                [t for t, _ in suspects],
                [e for _, e in suspects] + (["failed again on rerun"] if confirmed else []),
                next_step="tell_author",
                calls=calls,
            )
        return Diagnosis(
            "flaky", 0.85, [t for t, _ in flaky], [e for _, e in flaky], next_step="rerun_failed_jobs", calls=calls
        )


def costs(calls: int) -> float:
    """What a model-driven diagnosis would cost (USD) at the configured model's list price, for budgeting."""
    cfg = settings.load("agent")
    pin, pout = cfg["prices_usd_per_mtok"][cfg["model"]]
    return calls * (3000 * pin + 400 * pout) / 1e6


def as_dict(d: Diagnosis) -> dict[str, Any]:
    return {
        "kind": d.kind,
        "confidence": d.confidence,
        "tests": d.tests,
        "evidence": d.evidence,
        "next_step": d.next_step,
    }
