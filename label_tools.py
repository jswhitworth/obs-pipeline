#!/usr/bin/env python3
"""Label import and consistency checks (design doc §7.2, §7.2.4).

Lives OUTSIDE obs_pipeline/ deliberately: invariant #5 says no code path in
the pipeline reads labels, and keeping this module out of the package makes
that structural rather than conventional.
"""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

LONG_HEADER = ["obs_id", "key_type", "key", "value", "status", "label_basis",
               "labeler_certainty", "blinded", "obs_hash", "labeled_by",
               "labeled_at"]

# The wide RENDERING's own columns (§7.2) -- not label keys. Everything else
# in that header must be a declared field, or it is a rules change request.
WIDE_METADATA_COLUMNS = frozenset({"obs_id", "entity_id", "confidence"})

# Label keys that exist only on the label side. `same_device` is a pairwise
# HUMAN judgement; claims.yaml#link_bases (mac, serial, hostname_token) is how
# the PIPELINE clusters. Drawing this from the rules would be wrong in a way
# that is hard to detect later -- a returned `mac` label is not a thing.
LINK_BASIS_LABEL_KEYS = ("same_device",)

# Ordered weakest-first, so a pair inherits its shakier half (§7.2.3).
CERTAINTY_ORDER = ["low", "medium", "high"]


def weaker_certainty(a: str, b: str) -> str:
    rank = {c: i for i, c in enumerate(CERTAINTY_ORDER)}
    return a if rank.get(a, 0) <= rank.get(b, 0) else b


class TransitivityError(Exception):
    """The label set is internally incoherent (§7.2.4)."""


class UndeclaredLabelColumnError(Exception):
    """The wide label file carries a column that is not a declared field.

    `for field in fields: row.get(field)` silently ignores anything not in
    the list, so a mistyped header drops that field's entire ground truth
    with no error -- and the resulting labels.csv still hashes, still loads
    and still verifies. §7.2.1 already says a labeler needing a VALUE
    outside the vocabulary is filing a rules change request; the same is
    true of a KEY.
    """


class LabelRegenerationError(Exception):
    """Re-importing the wide file would discard adjudicated ground truth.

    labels-initial.csv is entirely `payload_inference`, so regenerating over
    a set that has been adjudicated reverts human judgement to the inference
    it overruled -- silently, since the result is still a valid, hashable,
    loadable label file. Nothing downstream could tell.
    """


class ArchiveCollisionError(Exception):
    """An apply would overwrite an existing archived label set (§7.6).

    Raised BEFORE anything is written. The archive is the only artifact that
    makes a two-pass diff runnable without git archaeology, so clobbering one
    destroys exactly the audit trail it exists to be -- the same reasoning
    that makes run.py guard against run_id collisions.
    """


BASIS_PRECEDENCE = ("physical_inspection", "asset_inventory", "vendor_doc",
                    "payload_inference")


def label_identity(row) -> tuple:
    """What makes two label rows assertions about the SAME thing.

    For a field, the slot is `(obs_id, key)` -- one vendor per observation,
    so two rows there compete and precedence decides between them.

    For a link_basis they do NOT compete: `A~B` and `A~C` are both true at
    once, and the pair itself is the assertion, so `value` is part of the
    identity. Leaving it out collapses every pair an observation has into
    one -- silently, since the result still hashes, still loads, and still
    passes every check except `check_transitivity`.
    """
    base = (row["obs_id"], row["key_type"], row["key"])
    return base if row["key_type"] == "field" else base + (row["value"],)


def resolve_by_basis_precedence(labels) -> list[dict]:
    """§7.2.2 -- adjudication by a FLAT precedence order.

    This is intentionally not a scoring formula. Resist recursing the
    claim-scoring math (§2.3) onto labels: a flat precedence list is
    sufficient and stays legible, and ground truth that needs a numeric,
    combined-confidence model is no longer serving as ground truth.
    """
    rank = {b: i for i, b in enumerate(BASIS_PRECEDENCE)}
    grouped: dict[tuple, list[dict]] = {}
    for label in labels:
        grouped.setdefault(label_identity(label), []).append(label)

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


def import_wide_labels(wide_path, observations_path, out_path,
                       *, fields, force=False) -> list[dict]:
    """§7.2 -- import the wide RENDERING into the long-format schema.

    `fields` is `claims.yaml#fields`, passed in rather than kept as a
    private copy here. A stale copy of that vocabulary is this repo's
    recurring structural defect (found three times in the pipeline half),
    and on the label side it is worse: the list gates what may become
    ground truth, so a field added to claims.yaml would be resolved by the
    pipeline and scored against labels while being impossible to label.

    Required, not defaulted: a default that quietly reads `rules/` would
    reproduce the same hidden coupling in a new form.
    """
    # Refuse to regenerate over adjudicated ground truth. The wide file is
    # all `payload_inference` (§7.2), so overwriting a set that has been
    # adjudicated reverts exactly the human judgement that outranked it --
    # and leaves a file that still hashes and loads, so no later check
    # could catch it. `force` exists for a deliberate reset.
    if not force and Path(out_path).exists():
        advanced = sorted({
            r["obs_id"] for r in load_labels(out_path)
            if r.get("label_basis") != "payload_inference"
        })
        if advanced:
            raise LabelRegenerationError(
                f"{out_path} carries adjudicated labels on "
                f"{len(advanced)} observation(s) ({', '.join(advanced[:5])}"
                f"{'...' if len(advanced) > 5 else ''}) which this import "
                f"would revert to payload_inference. Archive it and pass "
                f"force=True if that is genuinely intended."
            )

    with open(observations_path, newline="", encoding="utf-8") as fh:
        obs_hashes = {r["obs_id"]: obs_hash(r) for r in csv.DictReader(fh)}
    with open(wide_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = list(reader.fieldnames or [])
        wide = list(reader)

    # Which columns carry labels is a property of THIS FILE; which keys may
    # be labels is a property of the rules. Intersect them, and refuse the
    # difference rather than dropping it silently.
    undeclared = [c for c in header
                  if c not in WIDE_METADATA_COLUMNS and c not in fields]
    if undeclared:
        raise UndeclaredLabelColumnError(
            f"{wide_path} has column(s) {undeclared} that are neither wide-file "
            f"metadata {sorted(WIDE_METADATA_COLUMNS)} nor declared fields "
            f"{list(fields)}. A column that is silently ignored drops that "
            f"field's ground truth with no error; declare it in "
            f"claims.yaml#fields or correct the header."
        )
    # Declared but absent from this file is fine -- a coverage gap, not an
    # error. There is simply no data for it yet.
    label_columns = [f for f in fields if f in header]

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
        for field in label_columns:
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
                "labeler_certainty": weaker_certainty(certainty_by_obs[a],
                                                      certainty_by_obs[b]),
                "blinded": "false",
                "obs_hash": obs_hashes[a],
                "labeled_by": "import:labels-initial.csv", "labeled_at": "",
            })

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Importing into a directory with no VERSION is the BIRTH of a label
    # set, so seed one rather than crashing -- the same self-healing
    # run.py applies to a missing runs/ on a fresh checkout.
    version_file = out_path.parent / "VERSION"
    if not version_file.exists():
        version_file.write_text("0.1.0\n", encoding="utf-8")

    ordered = sorted(rows, key=lambda r: (r["obs_id"], r["key_type"],
                                          r["key"], r["value"]))

    if not out_path.exists():
        # First import: there is nothing to archive and nothing to diff
        # against, so the seeded version stands. State is still recorded --
        # a check that never says True is a check people learn to ignore.
        _write_labels(out_path, ordered)
        write_label_state(out_path)
        return ordered

    # A re-import CHANGES ground truth, so it is versioned exactly like an
    # apply. Writing the new content and then stamping state against the
    # unchanged VERSION is how two evals could carry the same
    # labels_version, both `verified`, and different labels_hash.
    before = {label_identity(r): r for r in load_labels(out_path)}
    after = {label_identity(r): r for r in ordered}
    changes = []
    for identity in sorted(before.keys() | after.keys()):
        old, new = before.get(identity), after.get(identity)
        if old is None:
            changes.append(_change(identity, None, new, "added"))
        elif new is None:
            changes.append(_change(identity, old, old, "removed"))
        elif old["value"] != new["value"]:
            changes.append(_change(identity, old, new, "value_changed"))

    _commit_labels(out_path, ordered, changes, source_run_id=str(wide_path),
                   applied_at=None,
                   version_before=version_file.read_text(
                       encoding="utf-8").strip(),
                   hash_before=labels_hash(out_path))
    return ordered


def load_labels(path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# --- write-back and label-set versioning (§7.2.2, §7.6) -------------------
#
# The mirror of the rules-side machinery in loader.py: an immutable archive
# of what was replaced, an append-only journal, and "the hash polices the
# version". Labels move forward the way runs and rules do -- deliberately,
# on a recorded decision, never by hand-editing the file everything scores
# against.

# Ordered weakest-first so the strongest change in a batch sets the bump.
_BUMP_ORDER = ["patch", "minor", "major"]

# What each kind of change does to a consumer of the label set. The policy
# lives here, not in whoever ran the import -- the same argument §7.6 makes
# for propose_bump on the rules side.
_CHANGE_BUMP = {
    "provenance_upgraded": "patch",   # same value, stronger basis: outcome-neutral,
                                      # but labels_hash moved so VERSION must too
    "corroborated": "patch",          # a second labeler at the same tier agreed
    "value_changed": "minor",         # eval outcomes can move
    "added": "minor",                 # a new eval denominator entry
    "removed": "major",               # denominators shrink; consumers lose a label
}


def _write_labels(path, rows) -> None:
    """One writer, one sort order. import_wide_labels and every apply must
    agree, or labels_hash starts depending on adjudication arrival order."""
    rows = sorted(rows, key=lambda r: (r["obs_id"], r["key_type"],
                                       r["key"], r["value"]))
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LONG_HEADER)
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in LONG_HEADER} for r in rows)


def propose_label_bump(changes) -> str | None:
    """§7.6 applied to labels -- derive the bump from the MEASURED change.

    Returns None when nothing changed: a version that moved on a no-op would
    make labels_hash and VERSION disagree about whether anything happened.
    """
    ranks = [_BUMP_ORDER.index(_CHANGE_BUMP[c["change"]]) for c in changes
             if c["change"] in _CHANGE_BUMP]
    return _BUMP_ORDER[max(ranks)] if ranks else None


def _bumped(version: str, bump: str) -> str:
    major, minor, patch = (int(p) for p in version.split("."))
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def _state_path(labels_path) -> Path:
    return Path(labels_path).parent / "last_label_state.json"


def read_label_state(labels_path) -> dict | None:
    path = _state_path(labels_path)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def labels_version_verified(labels_path) -> bool | None:
    """§6.2's discipline pointed at the other side: the hash polices the
    version.

    True  -- labels.csv is exactly what the last sanctioned write left.
    False -- it moved since, so someone edited ground truth outside the
             apply path. Not fatal, but it must never pass silently: the
             four-bucket diff assumes a frozen label set (§7.6).
    None  -- no state recorded yet. Report "unknown", never a confident True.
    """
    state = read_label_state(labels_path)
    if state is None:
        return None
    version = (Path(labels_path).parent / "VERSION").read_text(
        encoding="utf-8").strip()
    return (state.get("labels_hash") == labels_hash(labels_path)
            and state.get("version") == version)


def write_label_state(labels_path) -> dict:
    state = {
        "labels_hash": labels_hash(labels_path),
        "version": (Path(labels_path).parent / "VERSION").read_text(
            encoding="utf-8").strip(),
    }
    _state_path(labels_path).write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return state


def _commit_labels(labels_path, rows, changes, *, source_run_id, applied_at,
                   version_before, hash_before) -> dict:
    """Archive, write, bump, journal, record state -- in that order.

    Shared by both sanctioned writers. import_wide_labels going through a
    different path was how it managed to rewrite labels.csv and then stamp
    the NEW hash against the UNCHANGED version, leaving two evals able to
    carry the same labels_version, both `verified`, and different
    labels_hash -- the frozen-label-set assumption broken by the one check
    that exists to catch it.
    """
    bump = propose_label_bump(changes)
    result = {
        "applied": changes, "refused": [], "bump": bump,
        "version_before": version_before, "version_after": version_before,
        "archive": None, "labels_hash_before": hash_before,
        "labels_hash_after": hash_before,
    }
    if bump is None:
        return result

    labels_path = Path(labels_path)
    archive_dir = labels_path.parent / "archive"
    archive = archive_dir / f"{version_before}-labels.csv"
    if archive.exists():
        raise ArchiveCollisionError(
            f"{archive} already exists -- a second write from label version "
            f"{version_before} would overwrite the archived set that version "
            f"denotes. Nothing was written."
        )
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(labels_path.read_bytes())

    version_after = _bumped(version_before, bump)
    _write_labels(labels_path, rows)
    (labels_path.parent / "VERSION").write_text(version_after + "\n",
                                                encoding="utf-8")
    result.update(version_after=version_after, archive=str(archive),
                  labels_hash_after=labels_hash(labels_path))

    with open(labels_path.parent / "journal.jsonl", "a",
              encoding="utf-8") as fh:
        fh.write(json.dumps({
            "applied_at": applied_at or datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "source_run_id": source_run_id,
            "version_before": version_before,
            "version_after": version_after,
            "bump": bump,
            "archive": str(archive),
            "labels_hash_before": hash_before,
            "labels_hash_after": result["labels_hash_after"],
            "changes": changes,
        }, sort_keys=True) + "\n")

    write_label_state(labels_path)
    return result


def apply_adjudicated(accepted, labels_path, *, source_run_id=None,
                      applied_at=None) -> dict:
    """Write validated adjudicated labels back into labels.csv (§7.2.2).

    Closes the round trip FINDINGS.md §4 recorded as open: `accepted` rows
    from adjudicate.import_returned_labels are already in long-label schema,
    and this decides whether each one lands.

    The merge rule is not new. resolve_by_basis_precedence already implements
    §7.2.2's flat precedence, and it is asked ONCE per label identity about
    the existing row and every incoming row for it together. Two `disputed`
    rows back means the tiers tie and disagree -- precisely "a human
    adjudicator is required" -- so the whole group is refused rather than
    the tool inventing an adjudication.

    Resolving pairwise instead, one incoming row at a time against a
    mutating file, would let whichever row arrived first become ground
    truth and refuse the second as disputed. §7.2.3 routes medium/low
    certainty to DUAL labelling, so two rows for one key in one returned
    file is the designed workflow, not an edge case.

    Nothing is written unless something changed, and the archive is taken
    BEFORE the write.
    """
    labels_path = Path(labels_path)
    version_before = (labels_path.parent / "VERSION").read_text(
        encoding="utf-8").strip()
    hash_before = labels_hash(labels_path)

    by_key = {label_identity(r): r for r in load_labels(labels_path)}

    incoming_by_identity: dict[tuple, list[dict]] = {}
    for row in accepted:
        incoming_by_identity.setdefault(label_identity(row), []).append(row)

    applied: list[dict] = []
    refused: list[str] = []

    for identity, group in sorted(incoming_by_identity.items()):
        existing = by_key.get(identity)
        label = f"{identity[0]}.{identity[2]}"

        settled = resolve_by_basis_precedence(
            ([existing] if existing else []) + group)
        if len(settled) > 1:
            refused.append(
                f"{label}: disputed -- "
                f"{sorted({r['value'] for r in settled})} share basis "
                f"'{settled[0]['label_basis']}' and disagree. §7.2.2 requires "
                f"a human adjudicator; applying any one side would be this "
                f"tool inventing the adjudication."
            )
            continue

        winner = settled[0]
        if existing is None:
            by_key[identity] = dict(winner)
            applied.append(_change(identity, None, winner, "added"))
            continue

        if winner["value"] == existing["value"] and \
                winner["label_basis"] == existing["label_basis"]:
            # Same assertion at the same tier. That is corroboration -- the
            # entire payoff of dual labelling -- but ONLY from a different
            # labeler; the same one returning the same row again is a
            # re-apply, and must stay an idempotent no-op.
            corroborating = winner.get("status") == "agreed" and any(
                g.get("labeled_by") != existing.get("labeled_by")
                for g in group)
            if not corroborating:
                # Lost precedence, or already applied. Say so rather than
                # no-opping: a silent success would let a caller believe an
                # adjudication landed.
                refused.append(
                    f"{label}: no change -- existing '{existing['value']}' at "
                    f"basis '{existing['label_basis']}' is not superseded by "
                    f"'{group[0]['value']}' at '{group[0]['label_basis']}'"
                )
                continue
            change = "corroborated"
        else:
            change = ("value_changed" if winner["value"] != existing["value"]
                      else "provenance_upgraded")

        by_key[identity] = dict(winner)
        applied.append(_change(identity, existing, winner, change))

    result = _commit_labels(labels_path, by_key.values(), applied,
                            source_run_id=source_run_id, applied_at=applied_at,
                            version_before=version_before,
                            hash_before=hash_before)
    result["refused"] = refused
    return result


def _change(key, before, after, change) -> dict:
    return {
        "obs_id": key[0], "key_type": key[1], "key": key[2],
        "change": change,
        "before_value": before["value"] if before else None,
        "after_value": after["value"],
        "before_basis": before["label_basis"] if before else None,
        "after_basis": after["label_basis"],
        "before_status": before["status"] if before else None,
        "after_status": after["status"],
        "labeled_by": after.get("labeled_by", ""),
    }


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
    # Imported here rather than at module scope: the vocabulary is read at
    # the ENTRY POINT and threaded down, so importing the loader is a
    # property of the CLI, not of this module. Nothing that imports
    # label_tools as a library inherits a dependency on the rules loading.
    from obs_pipeline.loader import load_rules

    _rules = load_rules("rules", "obs-data/observations.csv")
    imported = import_wide_labels("labels/labels-initial.csv",
                                  "obs-data/observations.csv",
                                  "labels/labels.csv",
                                  fields=_rules.claims["fields"])
    problems = check_transitivity(imported)
    print(f"imported {len(imported)} labels")
    for p in problems:
        print(p)
    if problems:
        raise TransitivityError(f"{len(problems)} transitivity violations")
