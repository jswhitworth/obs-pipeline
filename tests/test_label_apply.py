# tests/test_label_apply.py
"""Write-back of adjudicated labels, and label-set versioning (§7.2.2, §7.6).

`import_returned_labels` validated returned labels and handed back accepted
rows, but nothing wrote them into labels.csv -- FINDINGS.md §4 recorded the
round trip as open at the far end. These tests pin the closing half:
precedence-governed merge, an archived snapshot of what was replaced, an
append-only journal, and a VERSION bump derived from the measured change
rather than from whoever ran the import.

Every test works on a tmp copy. A test that mutated the real labels/ would
be rewriting ground truth as a side effect of running the suite.
"""
import csv
import json
import shutil
from pathlib import Path

import pytest

from label_tools import (
    LONG_HEADER, ArchiveCollisionError, apply_adjudicated, labels_hash,
    labels_version_verified, load_labels, propose_label_bump, read_label_state,
)
from obs_pipeline.loader import load_rules

FIELDS = list(load_rules("rules", "obs-data/observations.csv").claims["fields"])

REAL_LABELS = Path("labels/labels.csv")


@pytest.fixture
def labels_dir(tmp_path):
    """A throwaway labels/ seeded from the real one."""
    d = tmp_path / "labels"
    d.mkdir()
    shutil.copy(REAL_LABELS, d / "labels.csv")
    (d / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    return d


def _row(obs_id, key, value, *, basis="physical_inspection",
         key_type="field", status="adjudicated"):
    return {
        "obs_id": obs_id, "key_type": key_type, "key": key, "value": value,
        "status": status, "label_basis": basis, "labeler_certainty": "high",
        "blinded": "true", "obs_hash": "sha256:unchecked",
        "labeled_by": "adjudicator", "labeled_at": "2026-08-05T00:00:00Z",
    }


def _find(path, obs_id, key):
    return next(r for r in load_labels(path)
                if r["obs_id"] == obs_id and r["key"] == key)


# --- merge semantics ------------------------------------------------------

def test_higher_basis_replaces_the_payload_inference_row(labels_dir):
    """§7.2.2 governs the merge -- the same flat precedence that decides
    between conflicting labels decides whether a returned one lands. No
    second adjudication rule."""
    labels = labels_dir / "labels.csv"
    before = _find(labels, "OBS-011", "vendor")
    assert before["label_basis"] == "payload_inference"
    assert before["value"] == "Hikvision"

    apply_adjudicated([_row("OBS-011", "vendor", "Dahua Technology")], labels)

    after = _find(labels, "OBS-011", "vendor")
    assert after["value"] == "Dahua Technology"
    assert after["label_basis"] == "physical_inspection"
    assert after["status"] == "adjudicated"


def test_one_row_per_key_survives_the_merge(labels_dir):
    """Replace, never append. Keeping both rows would force every consumer
    -- eval.py, export_packet's stickiness check -- to run precedence
    resolution on load."""
    labels = labels_dir / "labels.csv"
    apply_adjudicated([_row("OBS-011", "vendor", "Dahua Technology")], labels)
    matches = [r for r in load_labels(labels)
               if r["obs_id"] == "OBS-011" and r["key"] == "vendor"]
    assert len(matches) == 1


def test_same_tier_disagreement_is_refused_not_applied(labels_dir):
    """resolve_by_basis_precedence returns two `disputed` rows for a
    same-tier conflict, which is exactly the "a human is required" signal.
    Applying either side would be the tool inventing an adjudication."""
    labels = labels_dir / "labels.csv"
    apply_adjudicated([_row("OBS-011", "vendor", "Dahua Technology")], labels)
    result = apply_adjudicated(
        [_row("OBS-011", "vendor", "Hikvision")], labels)

    assert result["applied"] == []
    assert any("disputed" in r for r in result["refused"])
    assert _find(labels, "OBS-011", "vendor")["value"] == "Dahua Technology"


def test_lower_basis_cannot_overwrite_a_settled_row(labels_dir):
    """A returned row that loses precedence changes nothing, and says so.
    Silently no-opping would let a caller believe an adjudication landed."""
    labels = labels_dir / "labels.csv"
    apply_adjudicated([_row("OBS-011", "vendor", "Dahua Technology")], labels)
    result = apply_adjudicated(
        [_row("OBS-011", "vendor", "Bosch Security Systems",
              basis="vendor_doc")], labels)

    assert result["applied"] == []
    assert result["refused"]
    assert _find(labels, "OBS-011", "vendor")["value"] == "Dahua Technology"


def test_a_key_with_no_existing_row_is_added(labels_dir):
    """OBS-042 has no model label. A returned one is new ground truth, not a
    replacement."""
    labels = labels_dir / "labels.csv"
    assert not [r for r in load_labels(labels)
                if r["obs_id"] == "OBS-042" and r["key"] == "model"]

    result = apply_adjudicated([_row("OBS-042", "model", "DS-2CD2143G0-I")],
                               labels)

    assert len(result["applied"]) == 1
    assert result["applied"][0]["change"] == "added"
    assert _find(labels, "OBS-042", "model")["value"] == "DS-2CD2143G0-I"


def test_rows_stay_in_the_canonical_sort_order(labels_dir):
    """import_wide_labels writes sorted by (obs_id, key_type, key, value);
    an apply that appended in arrival order would make every subsequent
    labels_hash depend on adjudication order."""
    labels = labels_dir / "labels.csv"
    apply_adjudicated([_row("OBS-042", "model", "DS-2CD2143G0-I")], labels)
    rows = load_labels(labels)
    keys = [(r["obs_id"], r["key_type"], r["key"], r["value"]) for r in rows]
    assert keys == sorted(keys)


def test_reapplying_the_same_adjudication_is_a_no_op(labels_dir):
    """Idempotence: no change means no archive, no journal line, no bump.
    A version that moved on a no-op would make labels_hash and VERSION
    disagree about whether anything happened."""
    labels = labels_dir / "labels.csv"
    apply_adjudicated([_row("OBS-011", "vendor", "Dahua Technology")], labels)
    version_after_first = (labels_dir / "VERSION").read_text().strip()
    hash_after_first = labels_hash(labels)
    journal_lines = len((labels_dir / "journal.jsonl").read_text().splitlines())

    result = apply_adjudicated(
        [_row("OBS-011", "vendor", "Dahua Technology")], labels)

    assert result["applied"] == []
    assert result["bump"] is None
    assert (labels_dir / "VERSION").read_text().strip() == version_after_first
    assert labels_hash(labels) == hash_after_first
    assert len((labels_dir / "journal.jsonl").read_text().splitlines()) \
        == journal_lines


def test_a_rejected_row_does_not_block_its_valid_neighbours(labels_dir):
    """Each label is independent ground truth, so a partly-bad returned file
    applies the good rows and reports the rest."""
    labels = labels_dir / "labels.csv"
    apply_adjudicated([_row("OBS-011", "vendor", "Dahua Technology")], labels)
    result = apply_adjudicated([
        _row("OBS-011", "vendor", "Hikvision"),            # same tier: refused
        _row("OBS-012", "vendor", "Hikvision"),            # valid: basis rises
    ], labels)

    assert len(result["applied"]) == 1
    assert len(result["refused"]) == 1
    assert _find(labels, "OBS-012", "vendor")["label_basis"] \
        == "physical_inspection"


# --- archive --------------------------------------------------------------

def test_the_replaced_version_is_archived_before_the_write(labels_dir):
    """labels/archive/<old-version>-labels.csv makes the two-pass diff
    runnable without git archaeology."""
    labels = labels_dir / "labels.csv"
    original = labels.read_bytes()

    apply_adjudicated([_row("OBS-011", "vendor", "Dahua Technology")], labels)

    archived = labels_dir / "archive" / "0.1.0-labels.csv"
    assert archived.exists()
    assert archived.read_bytes() == original


def test_an_existing_archive_is_never_overwritten(labels_dir):
    """Clobbering an archive destroys the audit trail it exists to be. Same
    reasoning as run.py's run_id collision guard."""
    labels = labels_dir / "labels.csv"
    archive = labels_dir / "archive"
    archive.mkdir()
    (archive / "0.1.0-labels.csv").write_text("decoy\n", encoding="utf-8")

    with pytest.raises(ArchiveCollisionError, match="0.1.0"):
        apply_adjudicated([_row("OBS-011", "vendor", "Dahua Technology")],
                          labels)

    # Refused before writing anything: labels.csv and VERSION are untouched.
    assert (archive / "0.1.0-labels.csv").read_text() == "decoy\n"
    assert _find(labels, "OBS-011", "vendor")["label_basis"] \
        == "payload_inference"
    assert (labels_dir / "VERSION").read_text().strip() == "0.1.0"


# --- journal --------------------------------------------------------------

def test_the_journal_records_what_each_change_replaced(labels_dir):
    """The label set's trace: any current value walks back to who changed
    it, from which packet, replacing what."""
    labels = labels_dir / "labels.csv"
    before = _find(labels, "OBS-011", "vendor")["value"]

    apply_adjudicated([_row("OBS-011", "vendor", "Dahua Technology")], labels,
                      source_run_id="runs/2026-08-05T00:00:00Z-abc123")

    entries = [json.loads(l) for l in
               (labels_dir / "journal.jsonl").read_text().splitlines()]
    assert len(entries) == 1
    e = entries[0]
    assert e["version_before"] == "0.1.0"
    assert e["version_after"] == "0.2.0"
    assert e["bump"] == "minor"
    assert e["source_run_id"] == "runs/2026-08-05T00:00:00Z-abc123"
    assert e["archive"].endswith("0.1.0-labels.csv")

    change = next(c for c in e["changes"] if c["obs_id"] == "OBS-011")
    assert change["before_value"] == before
    assert change["after_value"] == "Dahua Technology"
    assert change["before_basis"] == "payload_inference"
    assert change["after_basis"] == "physical_inspection"
    assert change["labeled_by"] == "adjudicator"


def test_the_journal_brackets_the_change_with_both_hashes(labels_dir):
    """labels_hash before and after, so an entry can be tied to the exact
    eval manifests either side of it."""
    labels = labels_dir / "labels.csv"
    hash_before = labels_hash(labels)

    apply_adjudicated([_row("OBS-011", "vendor", "Dahua Technology")], labels)

    e = json.loads((labels_dir / "journal.jsonl").read_text().splitlines()[0])
    assert e["labels_hash_before"] == hash_before
    assert e["labels_hash_after"] == labels_hash(labels)
    assert e["labels_hash_before"] != e["labels_hash_after"]


def test_the_journal_is_append_only_across_applies(labels_dir):
    labels = labels_dir / "labels.csv"
    apply_adjudicated([_row("OBS-011", "vendor", "Dahua Technology")], labels)
    apply_adjudicated([_row("OBS-012", "vendor", "Hikvision")], labels)

    entries = [json.loads(l) for l in
               (labels_dir / "journal.jsonl").read_text().splitlines()]
    assert len(entries) == 2
    assert entries[0]["version_after"] == entries[1]["version_before"]


# --- version policy -------------------------------------------------------

def test_a_changed_value_is_a_minor_bump():
    """Eval outcomes can move, so consumers must see a new label version."""
    assert propose_label_bump([{"change": "value_changed"}]) == "minor"


def test_a_provenance_only_upgrade_is_a_patch():
    """Same value, stronger basis. Outcome-neutral -- but labels_hash moved,
    so VERSION must move too or the hash-polices-version check trips."""
    assert propose_label_bump([{"change": "provenance_upgraded"}]) == "patch"


def test_an_added_label_is_a_minor_bump():
    """A new label adds an eval denominator entry."""
    assert propose_label_bump([{"change": "added"}]) == "minor"


def test_a_removed_label_is_a_major_bump():
    """Denominators shrink: consumers lose a label they scored against."""
    assert propose_label_bump([{"change": "removed"}]) == "major"


def test_the_strongest_change_in_a_batch_sets_the_bump():
    assert propose_label_bump([
        {"change": "provenance_upgraded"},
        {"change": "value_changed"},
    ]) == "minor"
    assert propose_label_bump([
        {"change": "value_changed"},
        {"change": "removed"},
    ]) == "major"


def test_no_changes_proposes_no_bump():
    assert propose_label_bump([]) is None


def test_a_provenance_only_apply_bumps_the_patch_digit(labels_dir):
    """End to end: the same value at a higher basis moves 0.1.0 -> 0.1.1."""
    labels = labels_dir / "labels.csv"
    current = _find(labels, "OBS-011", "vendor")["value"]

    result = apply_adjudicated([_row("OBS-011", "vendor", current)], labels)

    assert result["bump"] == "patch"
    assert (labels_dir / "VERSION").read_text().strip() == "0.1.1"
    assert result["applied"][0]["change"] == "provenance_upgraded"


# --- hash polices version -------------------------------------------------

def test_apply_records_the_state_it_left_behind(labels_dir):
    """The mirror of runs/last_rules_state.json, pointed at the other side."""
    labels = labels_dir / "labels.csv"
    apply_adjudicated([_row("OBS-011", "vendor", "Dahua Technology")], labels)

    state = read_label_state(labels)
    assert state["version"] == "0.2.0"
    assert state["labels_hash"] == labels_hash(labels)
    assert labels_version_verified(labels) is True


def test_a_hand_edit_outside_the_apply_path_reads_unverified(labels_dir):
    """§6.2's discipline applied to labels: the hash polices the version.
    Editing labels.csv directly is exactly the contamination invariant #6
    exists to catch, and it must not pass silently."""
    labels = labels_dir / "labels.csv"
    apply_adjudicated([_row("OBS-011", "vendor", "Dahua Technology")], labels)

    rows = load_labels(labels)
    rows[0]["value"] = "tampered"
    with open(labels, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LONG_HEADER)
        w.writeheader()
        w.writerows(rows)

    assert labels_version_verified(labels) is False


def test_an_unrecorded_label_set_is_unknown_not_verified(labels_dir):
    """No state file yet -- report "unknown", never a confident True."""
    assert labels_version_verified(labels_dir / "labels.csv") is None


def test_importing_the_wide_file_records_state_too(tmp_path):
    """import_wide_labels is the other sanctioned writer. Without this, a
    fresh `python3 label_tools.py` leaves the label set reading `unknown`
    forever, and the check becomes something you learn to ignore."""
    from label_tools import import_wide_labels

    d = tmp_path / "labels"
    d.mkdir()
    (d / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    out = d / "labels.csv"

    import_wide_labels("labels/labels-initial.csv",
                       "obs-data/observations.csv", out, fields=FIELDS)

    assert labels_version_verified(out) is True


def test_reimport_refuses_to_destroy_applied_adjudications(labels_dir):
    """The regeneration hazard. `python3 label_tools.py` rebuilds labels.csv
    from labels-initial.csv, which is all `payload_inference` -- so after an
    adjudication has landed, a routine re-import would silently revert human
    ground truth to the inference it overruled. Nothing else in the system
    would notice: the file is still valid, still hashes, still loads.
    """
    from label_tools import LabelRegenerationError, import_wide_labels

    labels = labels_dir / "labels.csv"
    apply_adjudicated([_row("OBS-011", "vendor", "Dahua Technology")], labels)

    with pytest.raises(LabelRegenerationError, match="OBS-011"):
        import_wide_labels("labels/labels-initial.csv",
                           "obs-data/observations.csv", labels, fields=FIELDS)

    assert _find(labels, "OBS-011", "vendor")["value"] == "Dahua Technology"


def test_reimport_over_an_unadjudicated_set_is_allowed(labels_dir):
    """The guard must not block the ordinary workflow: re-importing after
    editing labels-initial.csv, before any adjudication has landed."""
    from label_tools import import_wide_labels

    labels = labels_dir / "labels.csv"
    rows = import_wide_labels("labels/labels-initial.csv",
                              "obs-data/observations.csv", labels, fields=FIELDS)
    assert rows


# --- link_basis rows are keyed by their pair, not by their key -----------

def test_applying_a_field_label_does_not_destroy_same_device_pairs(labels_dir):
    """`A~B` and `A~C` are BOTH true at once -- they are not competing
    assertions about one slot the way two vendors are. Keying link_basis
    rows by (obs_id, key_type, key) collapses every pair an observation
    has into one, so a single unrelated field adjudication silently
    deletes ground truth and leaves the set failing its own transitivity
    check."""
    labels = labels_dir / "labels.csv"
    before = {(r["obs_id"], r["value"]) for r in load_labels(labels)
              if r["key_type"] == "link_basis"}
    assert len(before) > 1

    apply_adjudicated([_row("OBS-011", "vendor", "Dahua Technology")], labels)

    after = {(r["obs_id"], r["value"]) for r in load_labels(labels)
             if r["key_type"] == "link_basis"}
    assert after == before


def test_a_field_apply_leaves_the_set_transitively_consistent(labels_dir):
    """The collapse above is silent: the result still hashes, still loads,
    and gets stamped labels_version_verified. Only transitivity notices."""
    from label_tools import check_transitivity

    labels = labels_dir / "labels.csv"
    assert check_transitivity(load_labels(labels)) == []
    apply_adjudicated([_row("OBS-011", "vendor", "Dahua Technology")], labels)
    assert check_transitivity(load_labels(labels)) == []


def test_a_new_same_device_pair_adds_rather_than_replacing(labels_dir):
    """An adjudicator asserting OBS-001~OBS-050 is adding a pair, not
    overwriting the pairs OBS-001 already has."""
    labels = labels_dir / "labels.csv"
    existing = {r["value"] for r in load_labels(labels)
                if r["obs_id"] == "OBS-001" and r["key"] == "same_device"}
    assert existing

    result = apply_adjudicated(
        [_row("OBS-001", "same_device", "OBS-050", key_type="link_basis")],
        labels)

    assert result["applied"][0]["change"] == "added"
    after = {r["value"] for r in load_labels(labels)
             if r["obs_id"] == "OBS-001" and r["key"] == "same_device"}
    assert after == existing | {"OBS-050"}


# --- in-batch conflicts ---------------------------------------------------

def test_two_labelers_disagreeing_in_one_batch_is_order_independent(labels_dir):
    """§7.2.3 routes medium/low to DUAL labelling, so two rows for one key
    in one returned file is the designed workflow. Resolving them pairwise
    against the file, one at a time, makes whichever arrives first become
    ground truth -- the tool inventing the adjudication that §7.2.2 says
    requires a human."""
    labels = labels_dir / "labels.csv"
    a = _row("OBS-011", "vendor", "Dahua Technology")
    b = _row("OBS-011", "vendor", "Bosch Security Systems")

    forward = apply_adjudicated([dict(a), dict(b)], labels)
    assert forward["applied"] == []
    assert any("disputed" in r for r in forward["refused"])

    reverse = apply_adjudicated([dict(b), dict(a)], labels)
    assert reverse["applied"] == []
    assert _find(labels, "OBS-011", "vendor")["value"] == "Hikvision"


def test_two_labelers_agreeing_in_one_batch_apply_once(labels_dir):
    """The other half of dual labelling: same value from two labelers is
    corroboration, and must land."""
    labels = labels_dir / "labels.csv"
    result = apply_adjudicated([
        {**_row("OBS-011", "vendor", "Dahua Technology"), "labeled_by": "a"},
        {**_row("OBS-011", "vendor", "Dahua Technology"), "labeled_by": "b"},
    ], labels)

    assert len(result["applied"]) == 1
    assert _find(labels, "OBS-011", "vendor")["value"] == "Dahua Technology"


def test_independent_corroboration_at_the_same_tier_is_recorded(labels_dir):
    """resolve_by_basis_precedence returns `agreed` when two labelers at one
    tier back the same value -- the entire payoff of dual labelling.
    Discarding it as "no change" leaves adjudicate.STICKY_TIERS's `agreed`
    tier dead code that nothing can ever produce."""
    labels = labels_dir / "labels.csv"
    apply_adjudicated(
        [{**_row("OBS-011", "vendor", "Dahua Technology"), "labeled_by": "a"}],
        labels)

    result = apply_adjudicated(
        [{**_row("OBS-011", "vendor", "Dahua Technology"), "labeled_by": "b"}],
        labels)

    assert len(result["applied"]) == 1
    assert result["applied"][0]["change"] == "corroborated"
    assert result["bump"] == "patch"
    assert _find(labels, "OBS-011", "vendor")["status"] == "agreed"


# --- import_wide_labels is a versioned writer too -------------------------

def test_reimport_that_changes_content_bumps_and_archives(labels_dir):
    """import_wide_labels rewrote labels.csv and then stamped state with
    the NEW hash against the UNCHANGED version -- so two evals could carry
    the same labels_version, both verified, and different labels_hash. That
    is the frozen-label-set assumption broken by the one check that exists
    to catch it."""
    import csv as _csv

    from label_tools import import_wide_labels

    labels = labels_dir / "labels.csv"
    wide = labels_dir / "wide.csv"
    with open("labels/labels-initial.csv", newline="", encoding="utf-8") as fh:
        rows = list(_csv.DictReader(fh))
        header = list(rows[0])
    rows[0]["vendor"] = "Bosch Security Systems"
    with open(wide, "w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        w.writerows(rows)

    import_wide_labels(wide, "obs-data/observations.csv", labels, fields=FIELDS)

    assert (labels_dir / "VERSION").read_text().strip() != "0.1.0"
    assert (labels_dir / "archive" / "0.1.0-labels.csv").exists()
    assert (labels_dir / "journal.jsonl").exists()
    assert labels_version_verified(labels) is True


def test_reimport_with_no_content_change_does_not_bump(labels_dir):
    from label_tools import import_wide_labels

    labels = labels_dir / "labels.csv"
    import_wide_labels("labels/labels-initial.csv",
                       "obs-data/observations.csv", labels, fields=FIELDS)
    assert (labels_dir / "VERSION").read_text().strip() == "0.1.0"


def test_a_blanked_wide_cell_removes_a_label_and_is_a_major_bump(labels_dir):
    """A removal shrinks eval denominators, so consumers must see a major
    bump. This is also what makes the `removed` policy reachable rather
    than documented-but-unproducible."""
    import csv as _csv

    from label_tools import import_wide_labels

    labels = labels_dir / "labels.csv"
    wide = labels_dir / "wide.csv"
    with open("labels/labels-initial.csv", newline="", encoding="utf-8") as fh:
        rows = list(_csv.DictReader(fh))
        header = list(rows[0])
    target = rows[0]["obs_id"]
    rows[0]["vendor"] = ""
    with open(wide, "w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        w.writerows(rows)

    import_wide_labels(wide, "obs-data/observations.csv", labels, fields=FIELDS)

    assert (labels_dir / "VERSION").read_text().strip() == "1.0.0"
    assert not [r for r in load_labels(labels)
                if r["obs_id"] == target and r["key"] == "vendor"]


# --- the untested half of the verified check ------------------------------

def test_a_hand_edited_VERSION_also_reads_unverified(labels_dir):
    """The hash half was pinned; the version half was not. Deleting the
    version comparison from labels_version_verified left the whole suite
    green -- and the version dimension is exactly what a regeneration
    without a bump breaks."""
    labels = labels_dir / "labels.csv"
    apply_adjudicated([_row("OBS-011", "vendor", "Dahua Technology")], labels)
    assert labels_version_verified(labels) is True

    (labels_dir / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    assert labels_version_verified(labels) is False
