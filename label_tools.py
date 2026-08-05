#!/usr/bin/env python3
"""Label import and consistency checks (design doc §7.2, §7.2.4).

Lives OUTSIDE obs_pipeline/ deliberately: invariant #5 says no code path in
the pipeline reads labels, and keeping this module out of the package makes
that structural rather than conventional.
"""
from __future__ import annotations

import csv
import hashlib
from itertools import combinations
from pathlib import Path

LONG_HEADER = ["obs_id", "key_type", "key", "value", "status", "label_basis",
               "labeler_certainty", "blinded", "obs_hash", "labeled_by",
               "labeled_at"]

WIDE_FIELDS = ["vendor", "model", "device_type", "firmware"]

# Ordered weakest-first, so a pair inherits its shakier half (§7.2.3).
CERTAINTY_ORDER = ["low", "medium", "high"]


def _weaker(a: str, b: str) -> str:
    rank = {c: i for i, c in enumerate(CERTAINTY_ORDER)}
    return a if rank.get(a, 0) <= rank.get(b, 0) else b


class TransitivityError(Exception):
    """The label set is internally incoherent (§7.2.4)."""


BASIS_PRECEDENCE = ("physical_inspection", "asset_inventory", "vendor_doc",
                    "payload_inference")


def resolve_by_basis_precedence(labels) -> list[dict]:
    """§7.2.2 -- adjudication by a FLAT precedence order.

    This is intentionally not a scoring formula. Resist recursing the
    claim-scoring math (§2.3) onto labels: a flat precedence list is
    sufficient and stays legible, and ground truth that needs a numeric,
    combined-confidence model is no longer serving as ground truth.
    """
    rank = {b: i for i, b in enumerate(BASIS_PRECEDENCE)}
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    for label in labels:
        grouped.setdefault(
            (label["obs_id"], label["key_type"], label["key"]), []
        ).append(label)

    out: list[dict] = []
    for _, group in sorted(grouped.items()):
        best_tier = min(rank.get(l["label_basis"], len(rank)) for l in group)
        top = [l for l in group if rank.get(l["label_basis"], len(rank)) == best_tier]
        values = {l["value"] for l in top}

        if len(values) == 1:
            winner = dict(top[0])
            # A single tier-mate agreeing is `agreed`; a lower tier overruled
            # is `adjudicated`.
            winner["status"] = "adjudicated" if len(group) > len(top) or len(top) > 1 \
                else winner.get("status", "proposed")
            if len(top) > 1 and len(group) == len(top):
                winner["status"] = "agreed"
            out.append(winner)
        else:
            # Same tier, genuine disagreement: a human adjudicator is required.
            for l in top:
                out.append({**l, "status": "disputed"})
    return sorted(out, key=lambda r: (r["obs_id"], r["key_type"], r["key"],
                                      r["value"]))


def validate_against_vocabulary(labels, vocab, rules) -> list[str]:
    """§7.2.1 kind 1 -- vocabulary disagreement is a RULES defect, not a
    labeling one.

    Two labelers writing `Hikvision` and `HIKVISION` agree about the device and
    differ on the string. Adjudicating that case by case papers over a gap in
    claims.yaml vocabulary or normalization.yaml. A labeler needing a value
    outside the vocabulary is filing a rules change request, not a label.
    """
    closed = rules.claims.get("closed_vocabulary_fields", {})
    problems: list[str] = []
    for label in labels:
        if label["key_type"] != "field" or label["key"] not in closed:
            continue          # open vocabulary: model, firmware
        column = closed[label["key"]]["vocab_column"]
        pool = vocab.vendors if column == "vendor" else vocab.device_types
        if label["value"] not in pool:
            problems.append(
                f"{label['obs_id']}.{label['key']}: '{label['value']}' is not "
                f"in canonical_vocab.csv -- this is a rules change request "
                f"(claims.yaml vocabulary or normalization.yaml alias), "
                f"not a label (§7.2.1)"
            )
    return problems


def obs_hash(row: dict) -> str:
    payload = "|".join(str(row.get(k, "")) for k in sorted(row))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def labels_hash(path) -> str:
    text = Path(path).read_text(encoding="utf-8").replace("\r\n", "\n").strip() + "\n"
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def import_wide_labels(wide_path, observations_path, out_path) -> list[dict]:
    with open(observations_path, newline="", encoding="utf-8") as fh:
        obs_hashes = {r["obs_id"]: obs_hash(r) for r in csv.DictReader(fh)}
    with open(wide_path, newline="", encoding="utf-8") as fh:
        wide = list(csv.DictReader(fh))

    rows: list[dict] = []
    by_entity: dict[str, list[str]] = {}
    certainty_by_obs: dict[str, str] = {}

    for r in wide:
        obs_id = r["obs_id"]
        # The wide file's `confidence` column is the LABELER's difficulty
        # assessment (§7.2.3), not a pipeline confidence.
        certainty = (r.get("confidence") or "").strip().lower()
        certainty_by_obs[obs_id] = certainty
        base = {
            "obs_id": obs_id,
            "status": "proposed",             # §7.2 -- stamped honestly
            "label_basis": "payload_inference",
            "labeler_certainty": certainty,
            "blinded": "false",
            "obs_hash": obs_hashes[obs_id],
            "labeled_by": "import:labels-initial.csv",
            "labeled_at": "",
        }
        for field in WIDE_FIELDS:
            value = (r.get(field) or "").strip()
            if not value:
                continue              # a blank is NOT an assertion (§7.2)
            rows.append({**base, "key_type": "field", "key": field, "value": value})
        entity = (r.get("entity_id") or "").strip()
        if entity:
            by_entity.setdefault(entity, []).append(obs_id)

    # Entity ids become PAIRWISE same-device judgments: §2.4 says the harness
    # matches on the partition, never on ID strings.
    for entity, members in sorted(by_entity.items()):
        for a, b in combinations(sorted(members), 2):
            rows.append({
                "obs_id": a, "key_type": "link_basis", "key": "same_device",
                "value": b, "status": "proposed",
                "label_basis": "payload_inference",
                # The WEAKER of the two members' certainties, not a hardcoded
                # "high" and not whichever obs_id happens to sort first.
                #
                # A pair judgment is only as confident as its shakier half.
                # §7.2.3 routes `medium`/`low` to dual-labelling precisely
                # BECAUSE they are uncertain, so a pair touching an uncertain
                # observation must inherit that uncertainty rather than lose
                # it to alphabetical accident. Taking obs `a`'s value stamps 4
                # of the 7 real pairs `high` while their partner was rated
                # `medium` — e.g. (OBS-001, OBS-002) only because OBS-001
                # sorts first.
                "labeler_certainty": _weaker(certainty_by_obs[a],
                                             certainty_by_obs[b]),
                "blinded": "false",
                "obs_hash": obs_hashes[a],
                "labeled_by": "import:labels-initial.csv", "labeled_at": "",
            })

    rows.sort(key=lambda r: (r["obs_id"], r["key_type"], r["key"], r["value"]))
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LONG_HEADER)
        w.writeheader()
        w.writerows(rows)
    return rows


def load_labels(path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def check_transitivity(labels) -> list[str]:
    """§7.2.4 -- A~B and A~C but B!~C violates transitivity regardless of who
    is right, and detecting it requires no adjudicator."""
    same = {tuple(sorted([r["obs_id"], r["value"]]))
            for r in labels if r.get("key") == "same_device"}
    neighbors: dict[str, set[str]] = {}
    for a, b in same:
        neighbors.setdefault(a, set()).add(b)
        neighbors.setdefault(b, set()).add(a)

    problems: list[str] = []
    for node, adj in sorted(neighbors.items()):
        for x, y in combinations(sorted(adj), 2):
            if tuple(sorted([x, y])) not in same:
                problems.append(
                    f"transitivity: {node}~{x} and {node}~{y} but not {x}~{y}"
                )
    return sorted(set(problems))


if __name__ == "__main__":
    imported = import_wide_labels("labels/labels-initial.csv",
                                  "obs-data/observations.csv",
                                  "labels/labels.csv")
    problems = check_transitivity(imported)
    print(f"imported {len(imported)} labels")
    for p in problems:
        print(p)
    if problems:
        raise TransitivityError(f"{len(problems)} transitivity violations")
