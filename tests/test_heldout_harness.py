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
