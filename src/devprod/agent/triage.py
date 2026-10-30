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
        """`confirmed`: None on the first look; True/False after the confirming rerun failed/passed.

        Order: a dead runner explains the run; then failing tests decide if there are any; then an install step
        (an outage if many pull requests fail there, the change's own manifest if it touched one, the
        machine if neither), then a job that hung in a test file, and finally an unexplained failure,
        which is rerun once."""
        jobs = self.t.failed_jobs(run_id)
        calls = 1
        if not jobs:
            return Diagnosis(
                "infra", 0.3, evidence=["no failed job recorded"], next_step="rerun_failed_jobs", calls=calls
            )
        logs = "\n".join(j["log_tail"] for j in jobs)
        changes = self.t.pr_changes(pr)
        reach = set(changes["affected"])
        calls += 1
        hit = next((m for m in self.mk["infra"] if m in logs), None)
        install = any(j["step_failed"] == "install" for j in jobs)
        if hit and not install:  # a dead runner explains the run, whatever else failed beside it
            return Diagnosis(
                "infra",
                0.9,
                evidence=[f"runner failure in the log: '{hit}'"],
                next_step="rerun_failed_jobs",
                calls=calls,
            )
        tests = [
            (t, j["module"])
            for j in jobs
            if j["step_failed"] != "install"
            for t in self.t.failed_tests(j["log_tail"], j["module"])
        ]
        if tests:
            return self._tests(tests, reach, confirmed, calls)
        if install:
            return self._install(pr, logs, now, confirmed, calls + 1, hit)
        if any(m in logs for m in self.mk["hang"]):
            module = jobs[0]["module"]
            where = self.t.in_progress(logs, module) or module
            if module in reach:
                return self._suspect(
                    [f"{module}::{where}"],
                    [f"the job hung in `{where}`, in `{module}`, which this change reaches"],
                    confirmed,
                    calls,
                )
            return Diagnosis(
                "infra",
                0.6,
                evidence=[f"the job hung in `{where}`, in `{module}`, which this change cannot reach"],
                next_step="rerun_failed_jobs",
                calls=calls,
            )
        return Diagnosis(
            "infra",
            0.4,
            evidence=["the job failed without a failed test or a known marker"],
            next_step="rerun_failed_jobs",
            calls=calls,
        )

    def _install(
        self, pr: int, logs: str, now: float, confirmed: bool | None, calls: int, hit: str | None
    ) -> Diagnosis:
        others = self.t.recent_step_failures("install", now - 120, now)
        said = next((m for m in self.mk["dependency"] if m in logs), None)
        quote = [f"install failed: '{said}'"] if said else []
        if others >= self.cfg["outage_pull_requests"]:
            return Diagnosis(
                "dependency",
                0.95,
                evidence=[*quote, f"{others} pull requests failed at install in 2 h"],
                next_step="wait_and_rerun",
                calls=calls,
            )
        manifests = self.t.touches_manifest(pr)
        if manifests and not hit:
            return self._suspect(
                [],
                [
                    *quote,
                    f"only {others} pull request(s) failed at install, and this one changes "
                    + ", ".join(f"`{m}`" for m in manifests),
                ],
                confirmed,
                calls,
            )
        why = f"runner failure in the log: '{hit}'" if hit else "install failed for this pull request only"
        return Diagnosis(
            "infra",
            0.7 if hit else 0.55,
            evidence=[why, f"{others} pull request(s) failed at install in 2 h, and this change touches no manifest"],
            next_step="rerun_failed_jobs",
            calls=calls,
        )

    def _suspect(self, tests: list[str], evidence: list[str], confirmed: bool | None, calls: int) -> Diagnosis:
        if confirmed is None and self.cfg["confirm_regression_by_rerun"]:
            return Diagnosis("regression", 0.6, tests, evidence, next_step="rerun_to_confirm", calls=calls)
        if confirmed is False:
            return Diagnosis(
                "flaky",
                0.8,
                tests,
                [*evidence, "passed when rerun on the same commit"],
                next_step="propose_quarantine",
                calls=calls,
            )
        return Diagnosis(
            "regression",
            0.92,
            tests,
            [*evidence, *(["failed again on rerun"] if confirmed else [])],
            next_step="tell_author",
            calls=calls,
        )

    def _tests(self, tests: list[tuple[str, str]], reach: set[str], confirmed: bool | None, calls: int) -> Diagnosis:
        suspects, flaky = [], []
        for test, _job_module in tests:
            h = self.t.test_history(test)
            calls += 1
            module = test.split("::")[0]
            if h["quarantined"] or h["p_flaky"] >= 0.5:
                flaky.append((test, f"`{test}` flipped {h['flips']} times in {h['runs']} runs"))
            elif module in reach:
                suspects.append((test, f"`{test}` is in `{module}`, which this change reaches"))
            else:
                flaky.append((test, f"`{test}` is in `{module}`, which this change cannot reach"))
        if suspects:
            return self._suspect([t for t, _ in suspects], [e for _, e in suspects], confirmed, calls)
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
