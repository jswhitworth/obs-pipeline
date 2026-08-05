#!/usr/bin/env python3
"""Blind adjudication packet export and round-trip guards (design doc §7.7).

Invariant #6 is the mirror of #5: labels don't reach the runtime path, and
pipeline output doesn't reach hard-stratum label creation.

A human handed a plausible answer and asked "is this right?" agrees more often
than one asked "what is this?". Unblinded labeling drifts ground truth toward
whatever the pipeline already believes, after which Stages 1-4 measure
self-consistency rather than correctness -- worst precisely where the pipeline
is confidently wrong.

SELECTION may read pipeline output; PRESENTATION must not. This tool sits
outside the pipeline with read access to both sides, and emits an
evidence-only packet.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

from label_tools import obs_hash

PACKET_COLUMNS = ("obs_id", "obs_hash", "source", "raw_payload", "mac",
                  "hostname", "open_ports", "site")

HARD_STRATUM_CONFIDENCE = 0.6
STICKY_TIERS = ("adjudicated", "agreed")
TIER_ORDER = ["physical_inspection", "asset_inventory", "vendor_doc",
              "payload_inference"]


def _read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def export_packet(run_dir, observations_path, labels_path, out_dir) -> Path:
    resolutions = _read_csv(Path(run_dir) / "resolutions.csv")
    observations = {r["obs_id"]: r for r in _read_csv(observations_path)}

    settled = {
        r["obs_id"] for r in _read_csv(labels_path)
        if r.get("status") in STICKY_TIERS
        and r.get("label_basis") != "payload_inference"
    }

    # Selection reads confidence and cluster size to IDENTIFY the hard stratum.
    cluster_size: dict = {}
    for r in resolutions:
        cluster_size[r["entity_id"]] = cluster_size.get(r["entity_id"], 0) + 1

    selected = sorted(
        r["obs_id"] for r in resolutions
        if r["obs_id"] not in settled
        and (float(r["confidence"]) < HARD_STRATUM_CONFIDENCE
             or float(r["stability"]) < HARD_STRATUM_CONFIDENCE
             or cluster_size[r["entity_id"]] == 1
             and float(r["confidence"]) < 0.75)
    )

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    packet = out_dir / "packet.csv"

    # Presentation carries evidence only. No vendor, no model, no confidence,
    # no entity_id -- the column isn't hidden by policy, it isn't in the file.
    with open(packet, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(PACKET_COLUMNS))
        w.writeheader()
        for obs_id in selected:
            obs = observations[obs_id]
            w.writerow({
                "obs_id": obs_id,
                "obs_hash": obs_hash(obs),
                "source": obs["source"],
                "raw_payload": obs["raw_payload"],
                "mac": obs["mac"],
                "hostname": obs["hostname"],
                "open_ports": obs["open_ports"],
                "site": obs["site"],
            })
    return packet


def import_returned_labels(packet_path, returned_path, observations_path):
    packet = {r["obs_id"]: r for r in _read_csv(packet_path)}
    current = {r["obs_id"]: obs_hash(r) for r in _read_csv(observations_path)}

    accepted, rejected = [], []
    for row in _read_csv(returned_path):
        obs_id = row["obs_id"]
        if obs_id not in packet:
            rejected.append(
                f"{obs_id}: not in the packet it was exported from -- "
                f"adjudication done via a back channel is refused (§7.7)"
            )
            continue
        if row.get("obs_hash") != current.get(obs_id):
            rejected.append(
                f"{obs_id}: stale obs_hash -- the observations row changed "
                f"since the packet was exported, so the label is flagged "
                f"rather than silently merged (§7.7)"
            )
            continue
        accepted.append({
            "obs_id": obs_id,
            "key_type": row["key_type"],
            "key": row["key"],
            "value": row["value"],
            "status": "adjudicated",
            "label_basis": "physical_inspection",
            "labeler_certainty": row.get("labeler_certainty", "high"),
            "blinded": "true",       # recorded PER LABEL, not assumed per stratum
            "obs_hash": row["obs_hash"],
            "labeled_by": row.get("labeled_by", "adjudicator"),
            "labeled_at": row.get("labeled_at", ""),
        })
    return accepted, rejected


if __name__ == "__main__":
    run_dir = sys.argv[1]
    out = export_packet(run_dir, "obs-data/observations.csv",
                        "labels/labels.csv", f"adjudication/{Path(run_dir).name}")
    print(out)
