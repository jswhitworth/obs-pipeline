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

# §7.2.3 vs §7.7: the design document disagrees with itself about what
# defines the hard stratum, and §7.2.3 is the section that resolves it.
#
# §7.7 says the export tool "necessarily reads confidence and cluster size to
# identify the hard stratum". §7.2.3 opens by noting that "earlier drafts used
# 'hard stratum' for two unrelated things", disentangles them, and assigns
# blind adjudication to `labeler_certainty` -- "medium and low -> dual-label
# and blind-adjudicate" -- for a stated reason:
#
#   "it requires no pipeline run to compute, so labeling never waits on a
#    bootstrap run and the stratum doesn't thrash when scoring is recalibrated"
#
# That reason is decisive here. Selecting on entity confidence picks 68 of 74
# observations on this dataset, because the harmonic rollup is dragged down by
# device_type (every device_type claim scores 0.25, the port_signature base
# weight). A 92% "stratum" defeats §7.7's own economics -- "agreement is
# measured where it's informative... at a fraction of full dual-labeling
# cost" -- and would be re-drawn by every coefficient change.
#
# So certainty drives selection. Pipeline output is still read, but only to
# EXCLUDE what is already settled, never to choose what to include.
STRATUM_CERTAINTY = ("medium", "low")
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

    # The stratum is a property of LABELLING DIFFICULTY, known before any
    # pipeline run (§7.2.3). Pipeline output is read only to drop what is
    # already settled -- selection may read it, presentation may not.
    certainty = {}
    for row in _read_csv(labels_path):
        if row["key_type"] == "field":
            certainty[row["obs_id"]] = row["labeler_certainty"]

    selected = sorted(
        r["obs_id"] for r in resolutions
        if r["obs_id"] not in settled
        and certainty.get(r["obs_id"]) in STRATUM_CERTAINTY
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
        if row.get("key_type") not in ("field", "link_basis"):
            rejected.append(
                f"{obs_id}: key_type {row.get('key_type')!r} is not a declared "
                f"label kind"
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
