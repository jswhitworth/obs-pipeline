import csv

import pytest

from adjudicate import PACKET_COLUMNS, export_packet, import_returned_labels
from run import run_pipeline


@pytest.fixture(scope="module")
def packet(tmp_path_factory):
    root = tmp_path_factory.mktemp("adj")
    run_dir = run_pipeline("obs-data/observations.csv", "rules", root / "runs")
    return export_packet(run_dir, "obs-data/observations.csv",
                         "labels/labels.csv", root / "adjudication")


def _rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_packet_carries_evidence_columns_only(packet):
    """§7.7: no vendor, no model, no confidence, no entity_id. Blinding is
    STRUCTURAL rather than procedural -- the column isn't hidden by policy,
    it isn't in the file, so no one has to be trusted not to look."""
    header = set(_rows(packet)[0])
    assert header == set(PACKET_COLUMNS)
    for forbidden in ["vendor", "model", "device_type", "firmware",
                      "confidence", "stability", "entity_id"]:
        assert forbidden not in header


def test_packet_retains_the_join_key_but_not_the_answer(packet):
    """§7.7: the join key survives, the answer doesn't. `run_id` is carried by
    the packet's DIRECTORY (adjudication/<run_id>/packet.csv), not as a
    column; `obs_id` and `obs_hash` are what a returned row joins on."""
    row = _rows(packet)[0]
    assert row["obs_id"]
    assert row["obs_hash"].startswith("sha256:")


def test_selection_reads_pipeline_output_but_presentation_does_not(packet):
    """§7.7: the export tool necessarily reads confidence and cluster size to
    IDENTIFY the hard stratum. What it must not do is put those columns in
    front of the human."""
    rows = _rows(packet)
    assert 0 < len(rows) < 74     # a stratum, not everything
    assert "confidence" not in rows[0]


def test_out_of_packet_labels_are_refused(packet, tmp_path):
    """§7.7: catches adjudication done from a full export via a back channel."""
    returned = tmp_path / "returned.csv"
    with open(returned, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["obs_id", "key_type", "key", "value", "obs_hash"])
        w.writerow(["OBS-999", "field", "vendor", "Hikvision", "sha256:whatever"])
    accepted, rejected = import_returned_labels(
        packet, returned, "obs-data/observations.csv")
    assert accepted == []
    assert any("OBS-999" in r for r in rejected)


def test_stale_obs_hash_is_flagged_not_silently_merged(packet, tmp_path):
    """§7.7: bind to evidence, not just the run."""
    row = _rows(packet)[0]
    returned = tmp_path / "returned.csv"
    with open(returned, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["obs_id", "key_type", "key", "value", "obs_hash"])
        w.writerow([row["obs_id"], "field", "vendor", "Hikvision",
                    "sha256:stale0000"])
    accepted, rejected = import_returned_labels(
        packet, returned, "obs-data/observations.csv")
    assert accepted == []
    assert any("stale" in r.lower() for r in rejected)


def test_valid_return_is_accepted_and_marked_blinded(packet, tmp_path):
    row = _rows(packet)[0]
    returned = tmp_path / "returned.csv"
    with open(returned, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["obs_id", "key_type", "key", "value", "obs_hash"])
        w.writerow([row["obs_id"], "field", "vendor", "Hikvision",
                    row["obs_hash"]])
    accepted, rejected = import_returned_labels(
        packet, returned, "obs-data/observations.csv")
    assert rejected == []
    assert len(accepted) == 1
    assert accepted[0]["blinded"] == "true"
    assert accepted[0]["label_basis"] == "physical_inspection"


def test_the_stratum_is_labeling_difficulty_not_pipeline_confidence(packet):
    """§7.2.3: the stratum is `labeler_certainty`, which needs no pipeline run
    and does not thrash when scoring is recalibrated. Selecting on entity
    confidence instead picks 68 of 74 observations here, because the harmonic
    rollup is dragged down by device_type scoring 0.25 everywhere -- a 92%
    "stratum" that defeats the point of having one."""
    import csv as _csv
    with open("labels/labels.csv", newline="", encoding="utf-8") as fh:
        certainty = {r["obs_id"]: r["labeler_certainty"]
                     for r in _csv.DictReader(fh) if r["key_type"] == "field"}
    selected = {r["obs_id"] for r in _rows(packet)}
    assert selected
    assert all(certainty[o] in ("medium", "low") for o in selected)
    expected = {o for o, c in certainty.items() if c in ("medium", "low")}
    assert selected == expected
    assert len(selected) == 19


def test_a_returned_label_with_a_bogus_key_type_is_refused(packet, tmp_path):
    """The packet carries no answer, so a returned row's shape is unvalidated
    input from outside the system."""
    row = _rows(packet)[0]
    returned = tmp_path / "returned.csv"
    with open(returned, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["obs_id", "key_type", "key", "value", "obs_hash"])
        w.writerow([row["obs_id"], "not_a_kind", "vendor", "Hikvision",
                    row["obs_hash"]])
    accepted, rejected = import_returned_labels(
        packet, returned, "obs-data/observations.csv")
    assert accepted == []
    assert any("key_type" in r for r in rejected)


def test_adjudicating_one_field_does_not_strip_the_others(packet, tmp_path_factory):
    """Adjudication resolves per (obs_id, key_type, key); stickiness must too.
    Settling one field of an observation while its others remain unresolved
    must NOT remove that observation from the queue -- doing so drops the
    unresolved fields permanently, with no error and no recovery."""
    root = tmp_path_factory.mktemp("partial")
    run_dir = run_pipeline("obs-data/observations.csv", "rules", root / "runs")
    target = _rows(packet)[0]["obs_id"]

    with open("labels/labels.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
        header = list(rows[0])
    settled_one = False
    for r in rows:
        if r["obs_id"] == target and r["key"] == "device_type":
            r["status"] = "adjudicated"
            r["label_basis"] = "physical_inspection"
            settled_one = True
    assert settled_one, f"{target} has no device_type row to settle"

    labels_path = root / "labels.csv"
    with open(labels_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        w.writerows(rows)

    second = export_packet(run_dir, "obs-data/observations.csv", labels_path,
                           root / "adjudication")
    assert target in {r["obs_id"] for r in _rows(second)}, (
        f"{target} was dropped after settling only one of its fields"
    )


def test_a_returned_label_with_an_undeclared_key_is_refused(packet, tmp_path):
    """A returned label becomes ground truth, so its key must be declared."""
    row = _rows(packet)[0]
    returned = tmp_path / "returned.csv"
    with open(returned, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["obs_id", "key_type", "key", "value", "obs_hash"])
        w.writerow([row["obs_id"], "field", "not_a_real_field", "Hikvision",
                    row["obs_hash"]])
        w.writerow([row["obs_id"], "link_basis", "not_same_device", "OBS-002",
                    row["obs_hash"]])
    accepted, rejected = import_returned_labels(
        packet, returned, "obs-data/observations.csv")
    assert accepted == []
    assert len(rejected) == 2
    assert all("is not a declared" in r for r in rejected)


def test_sticky_labels_are_not_re_exported(packet, tmp_path_factory):
    """§7.7: once adjudicated at a given tier, an obs is not re-adjudicated
    unless its obs_hash changed or higher-tier evidence arrives. Otherwise
    every recalibration re-queues previously-settled observations."""
    root = tmp_path_factory.mktemp("sticky")
    run_dir = run_pipeline("obs-data/observations.csv", "rules", root / "runs")
    settled = _rows(packet)[0]["obs_id"]

    labels_path = root / "labels.csv"
    with open("labels/labels.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
        header = list(rows[0])
    for r in rows:
        if r["obs_id"] == settled:
            r["status"] = "adjudicated"
            r["label_basis"] = "physical_inspection"
    with open(labels_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        w.writerows(rows)

    second = export_packet(run_dir, "obs-data/observations.csv", labels_path,
                           root / "adjudication2")
    assert settled not in {r["obs_id"] for r in _rows(second)}


def test_settling_every_row_removes_an_observation_from_the_next_packet(
        tmp_path):
    """export_packet's stickiness check was unreachable until something
    could write `adjudicated` rows -- labels.csv was 100% payload_inference,
    so `_settled` was never true for any observation. This exercises the
    full loop: export, adjudicate, apply, re-export.

    An observation leaves the queue only when EVERY label row it has is
    settled: adjudicating one field must NOT strip that observation's other
    unresolved fields from re-queue.
    """
    import shutil

    from label_tools import apply_adjudicated, load_labels

    labels_dir = tmp_path / "labels"
    labels_dir.mkdir()
    labels = labels_dir / "labels.csv"
    shutil.copy("labels/labels.csv", labels)
    shutil.copy("labels/VERSION", labels_dir / "VERSION")

    run_dir = run_pipeline("obs-data/observations.csv", "rules",
                           tmp_path / "runs")
    obs = "obs-data/observations.csv"

    first = export_packet(run_dir, obs, labels, tmp_path / "pkt1")
    queued = {r["obs_id"] for r in _rows(first)}
    assert queued, "no observations in the blind-adjudication stratum"
    target = sorted(queued)[0]

    rows = [r for r in load_labels(labels) if r["obs_id"] == target]
    assert len(rows) > 1, "need a multi-row observation to test partiality"

    def _adjudicated(row):
        return {**row, "status": "adjudicated",
                "label_basis": "physical_inspection", "blinded": "true",
                "labeled_by": "adjudicator"}

    # Settle ONE row: the observation must still be queued.
    applied = apply_adjudicated([_adjudicated(rows[0])], labels)
    assert applied["applied"], applied["refused"]
    partial = export_packet(run_dir, obs, labels, tmp_path / "pkt2")
    assert target in {r["obs_id"] for r in _rows(partial)}

    # Settle the rest: now it leaves.
    apply_adjudicated([_adjudicated(r) for r in rows[1:]], labels)
    final = export_packet(run_dir, obs, labels, tmp_path / "pkt3")
    assert target not in {r["obs_id"] for r in _rows(final)}
