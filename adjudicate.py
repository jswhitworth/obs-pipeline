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
from collections import defaultdict
from pathlib import Path

from label_tools import BASIS_PRECEDENCE, WIDE_FIELDS, obs_hash

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
# One definition, imported. label_tools owns the precedence order because
# that is where adjudication resolves it; a second, byte-identical copy here
# is the same duplicated-vocabulary pattern a whole-branch review already
# found three times in the pipeline half.
TIER_ORDER = list(BASIS_PRECEDENCE)


def _read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def export_packet(run_dir, observations_path, labels_path, out_dir) -> Path:
    resolutions = _read_csv(Path(run_dir) / "resolutions.csv")
    observations = {r["obs_id"]: r for r in _read_csv(observations_path)}

    # Stickiness is per (obs_id, key_type, key), because adjudication is.
    # Computing it per obs_id and excluding the whole observation when ANY one
    # row is settled silently drops that observation's still-unresolved fields
    # from every future packet -- permanently, with no error and no recovery.
    # Adjudicating `device_type` alone would strip `vendor`, `model` and
    # `firmware` from re-queue, which is the ordinary workflow, not an edge
    # case.
    #
    # An observation therefore leaves the queue only when EVERY label row it
    # has is settled. A row adjudicated while `label_basis` is still
    # `payload_inference` does not count: nothing was upgraded past the
    # original inference, so it has not earned sticky status.
    label_rows = _read_csv(labels_path)
    rows_by_obs: dict = defaultdict(list)
    for row in label_rows:
        rows_by_obs[row["obs_id"]].append(row)

    def _settled(row) -> bool:
        return (row.get("status") in STICKY_TIERS
                and row.get("label_basis") != "payload_inference")

    settled = {
        obs_id for obs_id, rows in rows_by_obs.items()
        if rows and all(_settled(r) for r in rows)
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
        kind = row.get("key_type")
        if kind not in ("field", "link_basis"):
            rejected.append(
                f"{obs_id}: key_type {kind!r} is not a declared label kind"
            )
            continue
        # A returned label becomes ground truth, so its KEY must be declared
        # too -- not only its shape. Nothing downstream closes this: the
        # vocabulary check validates `value`, and skips any key outside the
        # closed set entirely.
        allowed = WIDE_FIELDS if kind == "field" else ["same_device"]
        if row.get("key") not in allowed:
            rejected.append(
                f"{obs_id}: key {row.get('key')!r} is not a declared "
                f"{kind} -- expected one of {sorted(allowed)}"
            )
            continue
        if not (row.get("value") or "").strip():
            rejected.append(f"{obs_id}: empty value for {row.get('key')!r}")
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
            "key_type": kind,
            "key": row.get("key"),
            "value": row.get("value"),
            "status": "adjudicated",
            "label_basis": "physical_inspection",
            "labeler_certainty": row.get("labeler_certainty", "high"),
            "blinded": "true",       # recorded PER LABEL, not assumed per stratum
            "obs_hash": row.get("obs_hash"),
            "labeled_by": row.get("labeled_by", "adjudicator"),
            "labeled_at": row.get("labeled_at", ""),
        })
    return accepted, rejected


if __name__ == "__main__":
    run_dir = sys.argv[1]
    out = export_packet(run_dir, "obs-data/observations.csv",
                        "labels/labels.csv", f"adjudication/{Path(run_dir).name}")
    print(out)
