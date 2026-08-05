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
    """§7.7: run_id and obs_hash are retained so returned labels join back
    cleanly -- the join key survives, the answer doesn't."""
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
