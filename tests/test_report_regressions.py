# tests/test_report_regressions.py
"""§7.6: 'Surface, don't bury' and 'Make it cumulative'."""
import csv
import json

import pytest

from obs_pipeline.report import write_report
from run import run_pipeline


@pytest.fixture()
def run_and_eval(tmp_path):
    run_dir = run_pipeline("obs-data/observations.csv", "rules", tmp_path / "runs")
    eval_dir = tmp_path / "evals" / "e1"
    eval_dir.mkdir(parents=True)
    (eval_dir / "four_bucket.json").write_text(json.dumps(
        {"fixed": 10, "broken": 2, "stable_correct": 50, "stable_incorrect": 3}))
    with open(eval_dir / "regressions.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["obs_id", "key_type", "key", "before", "after",
                    "changed_rule_files"])
        w.writerow(["OBS-012", "field", "vendor", "Hikvision", "Unknown",
                    "normalization.yaml"])
    return run_dir, eval_dir


def test_broken_counts_appear_in_the_report_not_only_a_side_file(run_and_eval):
    run_dir, eval_dir = run_and_eval
    write_report(run_dir, eval_dir=eval_dir)
    text = (run_dir / "REPORT.md").read_text()
    assert "broken" in text.lower()
    assert "2" in text


def test_the_full_broken_list_is_shown_not_just_the_count(run_and_eval):
    run_dir, eval_dir = run_and_eval
    write_report(run_dir, eval_dir=eval_dir)
    text = (run_dir / "REPORT.md").read_text()
    assert "OBS-012" in text
    assert "normalization.yaml" in text


def test_a_multi_file_rule_change_does_not_break_the_table(run_and_eval, tmp_path):
    """`changed_rule_files` is pipe-joined and `|` delimits markdown table
    cells, so an unescaped multi-file value truncates the row and silently
    drops every filename after the first — in exactly the case where knowing
    which rules changed matters most."""
    run_dir, eval_dir = run_and_eval
    with open(eval_dir / "regressions.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["obs_id", "key_type", "key", "before", "after",
                    "changed_rule_files"])
        w.writerow(["OBS-012", "field", "vendor", "Hikvision", "Unknown",
                    "extraction.yaml|normalization.yaml|scoring.yaml"])
    write_report(run_dir, eval_dir=eval_dir)
    # Scope to the regressions section: an obs_id is not unique to this table.
    # The same observation legitimately appears as a vocabulary-reject witness
    # earlier in the report, and matching that row instead would assert the
    # escaping of a table this test is not about.
    _before, _sep, tail = (run_dir / "REPORT.md").read_text().partition(
        "## Regressions since the baseline rule state")
    assert _sep, "regressions section missing"
    row = next(ln for ln in tail.splitlines() if "OBS-012" in ln)
    assert row.count("|") - row.count("\\|") == 6, row
    for name in ("extraction.yaml", "normalization.yaml", "scoring.yaml"):
        assert name in row


def test_report_without_an_eval_still_renders(run_and_eval):
    run_dir, _ = run_and_eval
    write_report(run_dir)
    assert (run_dir / "REPORT.md").exists()
