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


def _reject_rows(bundle):
    """The (field, value, count, [obs_id]) rows of the Vocabulary rejects table."""
    text = (bundle / "REPORT.md").read_text()
    rows = [ln for ln in text.splitlines()
            if ln.startswith("| `") and ln.rstrip().endswith("|")]
    parsed = []
    for ln in rows:
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) == 4 and cells[2].isdigit():
            obs = [o.strip(" `") for o in cells[3].split(",")]
            parsed.append((cells[0].strip("`"), cells[1].strip("`"),
                           int(cells[2]), obs))
    return parsed


def test_vocabulary_rejects_are_ranked_deterministically(bundle):
    """§6.3 calls this the expansion work queue, so it has to be scannable:
    frequency first, then field and value. Relying on an upstream file's row
    order for tie position is deterministic but arbitrary -- which goes for
    the witness list inside a row as much as for the rows themselves."""
    parsed = _reject_rows(bundle)
    assert parsed, "no vocabulary-reject rows found"
    keys = [(f, v, n) for f, v, n, _obs in parsed]
    assert keys == sorted(keys, key=lambda r: (-r[2], r[0], r[1]))
    for _f, _v, _n, obs in parsed:
        assert obs == sorted(obs)


def test_vocabulary_rejects_name_the_observations_that_witnessed_them(bundle):
    """A rejected value is only triageable against the raw observation behind
    it, so the queue names its witnesses -- and they must be the actual
    claim-level witnesses, not a count the reader has to re-join by hand."""
    import csv
    from collections import defaultdict
    expected = defaultdict(set)
    with open(bundle / "claims.csv", newline="") as fh:
        for c in csv.DictReader(fh):
            if c["in_vocab"] == "False":
                expected[(c["key"], c["value"])].add(c["obs_id"])
    assert expected, "fixture has no vocabulary rejects to report"
    rendered = {(f, v): (n, set(obs)) for f, v, n, obs in _reject_rows(bundle)}
    assert set(rendered) == set(expected)
    for key, obs_ids in expected.items():
        count, witnesses = rendered[key]
        assert witnesses == obs_ids
        assert count == len(obs_ids)


def test_vocabulary_rejects_name_the_field_the_gap_is_in(bundle):
    """A bare surface string does not say which vocabulary is short. The queue
    is only actionable if it names the canonical_vocab.csv column to extend,
    so every row carries a field that is a closed vocabulary column."""
    import csv
    with open("rules/canonical_vocab.csv", newline="") as fh:
        closed = set(next(csv.reader(fh)))
    fields = {field for field, _value, _n, _obs in _reject_rows(bundle)}
    assert fields, "no vocabulary-reject rows found"
    assert fields <= closed, f"rejects attributed to non-closed fields: {fields - closed}"


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
