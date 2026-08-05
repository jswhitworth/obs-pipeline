# tests/test_label_vocabulary.py
"""The declared field vocabulary reaches the label tooling (§7.2.1, §6.1).

`claims.yaml#fields` declares what the pipeline resolves. label_tools once
carried a private copy of that list, and this repo has found the same
rules-as-authority defect three separate times in the pipeline half -- three
modules holding stale copies of `claims.yaml#fields`, so adding a field
silently moved published numbers while stale consumers ignored the new
column.

The label side is worse, because the copy is the GATE on what may become
ground truth: a field added to claims.yaml would be resolved by the
pipeline, scored against labels, and yet impossible for an adjudicator to
label -- with no error anywhere.

`same_device` is deliberately NOT part of this. claims.yaml#link_bases is
[mac, serial, hostname_token] -- how the PIPELINE clusters. `same_device` is
a pairwise human judgement that exists only on the label side, so pointing
it at claims.yaml would be wrong in a way that is hard to detect later.
"""
import csv
import shutil

import pytest

from adjudicate import PACKET_COLUMNS, import_returned_labels
from label_tools import import_wide_labels, load_labels
from obs_pipeline.loader import load_rules

OBS = "obs-data/observations.csv"
DECLARED = list(load_rules("rules", OBS).claims["fields"])


def _wide(tmp_path, *, extra_column=None, drop=None):
    """A copy of labels-initial.csv, optionally with a column added/removed."""
    with open("labels/labels-initial.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    header = [c for c in rows[0] if c != drop]
    if extra_column:
        header.append(extra_column)
        for r in rows:
            r[extra_column] = "some_value"
    out = tmp_path / "wide.csv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return out


def _packet(tmp_path):
    out = tmp_path / "packet.csv"
    with open(OBS, newline="", encoding="utf-8") as fh:
        obs = list(csv.DictReader(fh))[0]
    from label_tools import obs_hash
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(PACKET_COLUMNS))
        w.writeheader()
        w.writerow({c: obs.get(c, "") for c in PACKET_COLUMNS}
                   | {"obs_id": obs["obs_id"], "obs_hash": obs_hash(obs)})
    return out, obs["obs_id"]


def _returned(tmp_path, obs_id, key, packet_path):
    packet = {r["obs_id"]: r for r in
              csv.DictReader(open(packet_path, newline="", encoding="utf-8"))}
    out = tmp_path / "returned.csv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["obs_id", "key_type", "key",
                                           "value", "obs_hash", "labeled_by"])
        w.writeheader()
        w.writerow({"obs_id": obs_id, "key_type": "field", "key": key,
                    "value": "some_value",
                    "obs_hash": packet[obs_id]["obs_hash"],
                    "labeled_by": "inspector"})
    return out


# --- the vocabulary is the rules', not label_tools' ----------------------

def test_wide_import_uses_the_declared_field_vocabulary(tmp_path):
    """Not a private copy that happens to match today."""
    labels = tmp_path / "labels.csv"
    import_wide_labels("labels/labels-initial.csv", OBS, labels,
                       fields=DECLARED)
    keys = {r["key"] for r in load_labels(labels) if r["key_type"] == "field"}
    assert keys <= set(DECLARED)
    assert keys


def test_a_newly_declared_field_becomes_labelable(tmp_path):
    """The failure this exists to prevent. Add a field to claims.yaml and
    give the wide file a column for it: it must import as ground truth
    rather than being silently ignored by a stale private list."""
    labels = tmp_path / "labels.csv"
    wide = _wide(tmp_path, extra_column="chassis_type")

    import_wide_labels(wide, OBS, labels, fields=DECLARED + ["chassis_type"])

    keys = {r["key"] for r in load_labels(labels) if r["key_type"] == "field"}
    assert "chassis_type" in keys


def test_a_newly_declared_field_becomes_adjudicatable(tmp_path):
    """The same failure at the other gate: a field the pipeline resolves and
    is scored on, that no adjudicator can return a label for."""
    packet, obs_id = _packet(tmp_path)
    returned = _returned(tmp_path, obs_id, "chassis_type", packet)

    accepted, rejected = import_returned_labels(
        packet, returned, OBS, fields=DECLARED + ["chassis_type"])

    assert not rejected, rejected
    assert accepted[0]["key"] == "chassis_type"


def test_an_undeclared_key_is_still_refused(tmp_path):
    """The gate must stay a gate -- widening it to "anything" would let a
    typo become ground truth."""
    packet, obs_id = _packet(tmp_path)
    returned = _returned(tmp_path, obs_id, "vendr", packet)

    accepted, rejected = import_returned_labels(packet, returned, OBS,
                                                fields=DECLARED)
    assert accepted == []
    assert any("vendr" in r for r in rejected)


# --- a typo'd wide column must not vanish silently -----------------------

def test_an_unknown_wide_column_is_rejected_rather_than_dropped(tmp_path):
    """`for field in FIELDS: r.get(field)` silently ignores any column not
    in the list, so a mistyped header drops that field's entire ground truth
    with no error -- and the resulting labels.csv still hashes, still loads
    and still verifies.

    §7.2.1 already says a labeler needing a value outside the vocabulary is
    filing a rules change request, not a label. The same is true of a KEY."""
    from label_tools import UndeclaredLabelColumnError

    labels = tmp_path / "labels.csv"
    wide = _wide(tmp_path, extra_column="vendr")

    with pytest.raises(UndeclaredLabelColumnError, match="vendr"):
        import_wide_labels(wide, OBS, labels, fields=DECLARED)


def test_wide_file_metadata_columns_are_not_mistaken_for_fields(tmp_path):
    """obs_id, entity_id and confidence are the wide RENDERING's own
    columns (§7.2), not label keys, and must not trip the check above."""
    labels = tmp_path / "labels.csv"
    import_wide_labels("labels/labels-initial.csv", OBS, labels,
                       fields=DECLARED)
    assert load_labels(labels)


def test_a_declared_field_absent_from_the_wide_file_is_fine(tmp_path):
    """Declared but not yet labelled is a coverage gap, not an error --
    there is simply no data for it."""
    labels = tmp_path / "labels.csv"
    wide = _wide(tmp_path, drop="firmware")

    import_wide_labels(wide, OBS, labels, fields=DECLARED)

    keys = {r["key"] for r in load_labels(labels) if r["key_type"] == "field"}
    assert "firmware" not in keys
    assert "vendor" in keys


# --- same_device stays a label-side concept ------------------------------

def test_same_device_is_not_drawn_from_the_rules_link_bases(tmp_path):
    """claims.yaml#link_bases is [mac, serial, hostname_token] -- how the
    PIPELINE clusters. A returned `same_device` judgement is a human
    pairwise assertion that exists only on the label side, and a returned
    `mac` label is not a thing."""
    rules = load_rules("rules", OBS)
    assert "same_device" not in rules.claims["link_bases"]

    packet, obs_id = _packet(tmp_path)
    returned = tmp_path / "returned.csv"
    packet_rows = {r["obs_id"]: r for r in
                   csv.DictReader(open(packet, newline="", encoding="utf-8"))}
    with open(returned, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["obs_id", "key_type", "key",
                                           "value", "obs_hash", "labeled_by"])
        w.writeheader()
        for key in ("same_device", "mac"):
            w.writerow({"obs_id": obs_id, "key_type": "link_basis",
                        "key": key, "value": "OBS-002",
                        "obs_hash": packet_rows[obs_id]["obs_hash"],
                        "labeled_by": "inspector"})

    accepted, rejected = import_returned_labels(packet, returned, OBS,
                                                fields=DECLARED)
    assert [a["key"] for a in accepted] == ["same_device"]
    assert any("mac" in r for r in rejected)
