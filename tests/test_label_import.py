# tests/test_label_import.py
import csv

import pytest

from label_tools import (
    check_transitivity, import_wide_labels, labels_hash, load_labels, obs_hash,
)
from obs_pipeline.loader import load_rules

FIELDS = list(load_rules("rules", "obs-data/observations.csv").claims["fields"])

WIDE = "labels/labels-initial.csv"
OBS = "obs-data/observations.csv"


@pytest.fixture(scope="module")
def labels(tmp_path_factory):
    out = tmp_path_factory.mktemp("labels") / "labels.csv"
    import_wide_labels(WIDE, OBS, out, fields=FIELDS)
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
    # FIELD rows only. An observation's labelling certainty is a property of
    # labelling that observation, and it lives on its field rows. A pair row
    # carries the certainty of a PAIR JUDGMENT — the weaker of two members —
    # which is a different quantity about a different thing.
    #
    # Taking every row and letting the last win silently conflates them:
    # rows sort by (obs_id, key_type, ...) and "field" < "link_basis", so a
    # pair row wins its obs_id and drags OBS-004/047/061 from `high` to
    # `medium`, reporting 52/17/5 for a distribution that never moved.
    by_obs = {r["obs_id"]: r["labeler_certainty"]
              for r in labels if r["key_type"] == "field"}
    assert sum(1 for v in by_obs.values() if v == "high") == 55
    assert sum(1 for v in by_obs.values() if v in ("medium", "low")) == 19


def test_a_pair_row_can_disagree_with_its_own_observations_certainty():
    """This is WHY the stratum must be scoped to field rows, stated as the
    property rather than as a restatement of the scoping.

    OBS-004 is labelled `high`, but its pair with OBS-005 (`medium`) is a
    `medium` JUDGMENT — two different quantities about two different things,
    on rows that share an obs_id. A map built over ALL rows lets the pair row
    win, because rows sort by (obs_id, key_type, ...) and "field" <
    "link_basis", and silently reports OBS-004 as `medium`.

    This fails before the weaker-certainty fix, when a pair row carried its
    origin observation's own value and the two could never disagree. A guard
    that scopes to field rows before comparing cannot fail either way, and so
    guards nothing."""
    labels = load_labels("labels/labels.csv")
    field = {r["obs_id"]: r["labeler_certainty"]
             for r in labels if r["key_type"] == "field"}
    pair = next(r for r in labels
                if r["key"] == "same_device" and r["obs_id"] == "OBS-004")
    assert field["OBS-004"] == "high"
    assert pair["labeler_certainty"] == "medium"


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


def test_a_pair_inherits_the_weaker_certainty_of_its_two_members():
    """§7.2.3: `medium`/`low` route to dual-labelling BECAUSE they are
    uncertain. OBS-001 is `high` and OBS-002 is `medium`; their pair must be
    `medium`, not `high` — a pair judgment is only as confident as its shakier
    half, and taking whichever obs_id sorts first would lose that."""
    wide = {r["obs_id"]: r["confidence"]
            for r in csv.DictReader(open(WIDE, newline="", encoding="utf-8"))}
    assert wide["OBS-001"] == "high" and wide["OBS-002"] == "medium"
    labels = load_labels("labels/labels.csv")
    pair = next(r for r in labels
                if r["key"] == "same_device"
                and {r["obs_id"], r["value"]} == {"OBS-001", "OBS-002"})
    assert pair["labeler_certainty"] == "medium"


def test_labels_hash_changes_with_content_and_is_stable(tmp_path):
    """The eval manifest versions BOTH sides, and this hash is the label
    half. An unstable or content-blind hash would let the harness compare
    two different label sets while reporting them identical."""
    a = tmp_path / "a.csv"
    a.write_text("obs_id,value\nOBS-001,Axis\n", encoding="utf-8")
    b = tmp_path / "b.csv"
    b.write_text("obs_id,value\nOBS-001,Dahua\n", encoding="utf-8")
    assert labels_hash(a) == labels_hash(a)
    assert labels_hash(a) != labels_hash(b)
    assert labels_hash(a).startswith("sha256:")


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
