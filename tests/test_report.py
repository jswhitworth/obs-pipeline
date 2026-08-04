# tests/test_report.py
import pytest

from obs_pipeline.report import write_report
from run import run_pipeline


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    return run_pipeline("obs-data/observations.csv", "rules",
                        tmp_path_factory.mktemp("runs"))


def test_report_is_written(bundle):
    assert (bundle / "REPORT.md").exists()


def test_report_is_regenerable_from_the_bundle_alone(bundle):
    """§3: REPORT.md is a build artifact derived from the structured files. If
    it is lost it can be regenerated; nothing downstream may depend on parsing
    it."""
    before = (bundle / "REPORT.md").read_text()
    (bundle / "REPORT.md").unlink()
    write_report(bundle)
    assert (bundle / "REPORT.md").read_text() == before


def test_report_names_the_rule_state_it_was_produced_under(bundle):
    text = (bundle / "REPORT.md").read_text()
    assert "0.1.0" in text
    assert "sha256:" in text


def test_report_surfaces_undecidable_and_unknown_counts(bundle):
    text = (bundle / "REPORT.md").read_text().lower()
    assert "undecidable" in text
    assert "unknown" in text
