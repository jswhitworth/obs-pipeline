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


def test_report_is_identical_after_the_bundle_moves(bundle, tmp_path):
    """§3: a report found on disk months later must tie back to the exact
    rules and input. Embedding the run directory would make an archived copy
    differ for a reason unrelated to the run, so the rendered text must depend
    only on the bundle's CONTENTS, not on where it happens to live."""
    moved = tmp_path / "elsewhere"
    moved.mkdir()
    for p in bundle.iterdir():
        (moved / p.name).write_bytes(p.read_bytes())
    (moved / "REPORT.md").unlink()
    write_report(moved)
    assert (moved / "REPORT.md").read_text() == (bundle / "REPORT.md").read_text()


def test_vocabulary_rejects_are_ranked_deterministically(bundle):
    """§6.3 calls this the expansion work queue, so it has to be scannable:
    frequency first, then value. Relying on an upstream file's row order for
    tie position is deterministic but arbitrary."""
    text = (bundle / "REPORT.md").read_text()
    rows = [ln for ln in text.splitlines()
            if ln.startswith("| `") and ln.rstrip().endswith("|")]
    parsed = []
    for ln in rows:
        cells = [c.strip(" `") for c in ln.strip("|").split("|")]
        if len(cells) == 2 and cells[1].isdigit():
            parsed.append((cells[0], int(cells[1])))
    assert parsed, "no vocabulary-reject rows found"
    assert parsed == sorted(parsed, key=lambda kv: (-kv[1], kv[0]))


def test_report_names_the_rule_state_it_was_produced_under(bundle):
    text = (bundle / "REPORT.md").read_text()
    # The declared version, not a pinned literal -- the assertion is that
    # the report NAMES it, and a literal breaks on every bump.
    from pathlib import Path
    assert Path("rules/VERSION").read_text().strip() in text
    assert "sha256:" in text


def test_report_surfaces_undecidable_and_unknown_counts(bundle):
    text = (bundle / "REPORT.md").read_text().lower()
    assert "undecidable" in text
    assert "unknown" in text
