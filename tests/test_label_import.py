# tests/test_label_import.py
import csv

import pytest

from label_tools import (
    check_transitivity, import_wide_labels, load_labels, obs_hash,
)

WIDE = "labels/labels-initial.csv"
OBS = "obs-data/observations.csv"


@pytest.fixture(scope="module")
def labels(tmp_path_factory):
    out = tmp_path_factory.mktemp("labels") / "labels.csv"
    import_wide_labels(WIDE, OBS, out)
    return load_labels(out)


def test_long_format_is_authoritative(labels):
    """§7.2: a wide per-observation file is a RENDERING of this schema, not
    the schema. Wide files carry no status, label_basis or blinded."""
    required = {"obs_id", "key_type", "key", "value", "status", "label_basis",
                "labeler_certainty", "blinded", "obs_hash", "labeled_by",
                "labeled_at"}
    assert required <= set(labels[0])


def test_one_row_per_asserted_field_not_per_observation(labels):
    assert len(labels) > 74
    assert {r["key_type"] for r in labels} <= {"field", "link_basis"}


def test_blank_cells_produce_no_row_at_all(labels):
    """§7.2: a blank is not an assertion. Blank cells mean 'not derivable from
    THIS payload' and are excluded from denominators -- never counted as
    incorrect."""
    with open(WIDE, newline="", encoding="utf-8") as fh:
        wide = {r["obs_id"]: r for r in csv.DictReader(fh)}
    assert wide["OBS-003"]["firmware"] == ""
    assert not [r for r in labels
                if r["obs_id"] == "OBS-003" and r["key"] == "firmware"]


def test_provenance_is_stamped_honestly_not_optimistically(labels):
    """§7.2: on import from a wide file, stamp status: proposed,
    label_basis: payload_inference, blinded: false."""
    assert {r["status"] for r in labels} == {"proposed"}
    assert {r["label_basis"] for r in labels} == {"payload_inference"}
    assert {r["blinded"] for r in labels} == {"false"}


def test_confidence_column_is_imported_as_labeler_certainty(labels):
    """§7.2.3: the wide file's high|medium|low column is the LABELER's own
    difficulty assessment, not a pipeline confidence."""
    assert {r["labeler_certainty"] for r in labels} <= {"high", "medium", "low"}
    mine = [r for r in labels if r["obs_id"] == "OBS-002"]
    assert all(r["labeler_certainty"] == "medium" for r in mine)


def test_dual_label_stratum_is_medium_plus_low(labels):
    """§7.2.3: high -> single-label; medium and low -> dual-label and
    blind-adjudicate. 55 high / 14 medium / 5 low in the initial file."""
    by_obs = {r["obs_id"]: r["labeler_certainty"] for r in labels}
    assert sum(1 for v in by_obs.values() if v == "high") == 55
    assert sum(1 for v in by_obs.values() if v in ("medium", "low")) == 19


def test_obs_hash_binds_each_label_to_the_evidence_it_was_made_against(labels):
    """§7.7 round-trip guard: a label whose obs_hash no longer matches the
    current observations row is stale."""
    assert all(r["obs_hash"].startswith("sha256:") for r in labels)
    assert len({r["obs_hash"] for r in labels}) == 74


def test_entity_id_becomes_pairwise_link_basis_labels(labels):
    """Stage 3 needs same-device judgments, not ID strings -- §2.4 says the
    harness matches on the PARTITION, never on ID strings."""
    ident = [r for r in labels if r["key_type"] == "link_basis"]
    assert ident
    assert {r["key"] for r in ident} == {"same_device"}
    pairs = {tuple(sorted([r["obs_id"], r["value"]])) for r in ident}
    assert ("OBS-001", "OBS-002") in pairs
    assert ("OBS-069", "OBS-074") in pairs


def test_positive_pair_count_matches_the_designs_stated_figure(labels):
    """§7.4: 68 entities over 74 observations, 63 singletons, 5
    multi-observation entities covering 11 observations -- roughly 5 positive
    pairs. Any Stage 3 threshold 'met' at that n is noise."""
    pairs = {tuple(sorted([r["obs_id"], r["value"]]))
             for r in labels if r["key"] == "same_device"}
    # 7, not 5: E-001 has THREE members and so contributes C(3,2)=3 pairs,
    # plus 1 each from E-002/E-052/E-066/E-074. §7.4 says "5 multi-observation
    # entities... yielding roughly 5 positive pairs", which conflates the
    # entity count with the pair count. The conclusion is unaffected — 7 is
    # still far below any level at which a pairwise rate means anything.
    assert len(pairs) == 7


def test_transitivity_violation_is_detected_mechanically():
    """§7.2.4: a labeler asserts A~B and A~C but B!~C. Detecting it requires
    no adjudicator, and it must run BEFORE the labels are used -- Stage 3
    validation against an inconsistent label set produces meaningless
    precision numbers."""
    bad = [
        {"obs_id": "A", "key_type": "link_basis", "key": "same_device",
         "value": "B", "status": "proposed"},
        {"obs_id": "A", "key_type": "link_basis", "key": "same_device",
         "value": "C", "status": "proposed"},
    ]
    problems = check_transitivity(bad)
    assert problems
    assert any("B" in p and "C" in p for p in problems)


def test_imported_labels_are_transitively_consistent(labels):
    assert check_transitivity(labels) == []
