"""The held-out harness on a small set written for it (the real sets are scored by `devprod heldout`)."""

from __future__ import annotations

import json
from pathlib import Path

from devprod import heldout, settings


def test_harness_scores_and_records_the_first_run_once(store, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "HELDOUT", Path(__file__).parent / "fixtures" / "heldout")
    monkeypatch.setattr(settings, "REPORTS", tmp_path)
    out = heldout.run("sample", store.modules())
    assert out["cases"] == 4 and out["kind_accuracy"] == 1.0 and out["next_step_accuracy"] == 1.0
    assert out["first_run"] is True and out["status"] == "heldout"
    first = (tmp_path / "heldout" / "sample" / "first_run.json").read_text()
    again = heldout.run("sample", store.modules())
    assert "first_run" not in again
    assert (tmp_path / "heldout" / "sample" / "first_run.json").read_text() == first  # never rewritten
    assert json.loads((tmp_path / "heldout" / "sample" / "latest.json").read_text())["cases"] == 4
    assert "kind right 100%" in heldout.summary("sample")
    assert heldout.summary("missing") == "`missing`: not run yet."


def test_studied_sets_stay_fixed():
    """Sets whose failures were studied are regression sets: the agent must keep passing them."""
    for folder in sorted(settings.HELDOUT.iterdir()):
        if heldout.status(folder.name) != "regression":
            continue
        cases = heldout.load(folder.name)
        modules = _repo_modules()
        right = sum(heldout.judge(c, modules)["kind_ok"] for c in cases)
        assert right / len(cases) >= 0.95, folder.name


def _repo_modules():
    from devprod.sim.repo import build

    return {
        m.name: {"language": m.language, "deps": m.deps, "owner": m.owner, "path": m.path} for m in build().values()
    }
