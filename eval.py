#!/usr/bin/env python3
"""The eval harness (design doc §7.6, §8.4).

Sits OUTSIDE the pipeline. Invokes run.py as a black box and scores its
outputs against labels.csv. It is not a flag on run.py: because the pipeline
has no code path that reads labels, ground truth cannot leak into scoring
(invariant #5), and the deterministic runtime path stays free of evaluation
logic.

eval.py has no RuleSet -- it scores a WRITTEN bundle, not a live pipeline
run -- so it never carries its own copy of the field vocabulary. Three
modules once carried private copies of claims.yaml's declared field list;
adding a field silently moved published numbers while stale consumers
ignored the new column (progress.md, "recurring structural defect"). Here
the field list is read from the bundle's own entities.csv header instead
(_fields_from_bundle), which also preserves claims.yaml's declared order
(vendor, model, firmware, device_type) for free, since that is the order
bundle.py wrote the columns in.
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

from label_tools import labels_hash, load_labels
from obs_pipeline.metrics import load_registry
from run import run_pipeline

UNDECIDABLE = "undecidable"


def _read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _row(metric, scope, value, n):
    return {"metric": metric, "scope": scope, "value": round(float(value), 6),
            "n": int(n)}


def _fields_from_bundle(run_dir) -> list[str]:
    """The field vocabulary, read from entities.csv's own header rather than
    a private copy (see module docstring). entities.csv's columns are
    `<field>` and `<field>_confidence` per field (plus run_id,
    derivation_step, entity_id, confidence, stability), so a header column
    counts as a field exactly when a matching `<column>_confidence` column
    also exists -- that pairing is unique to the per-field columns and
    survives in the header's own declared order.
    """
    with open(Path(run_dir) / "entities.csv", newline="", encoding="utf-8") as fh:
        header = next(csv.reader(fh))
    header_set = set(header)
    return [c for c in header if f"{c}_confidence" in header_set]


def score_against_labels(run_dir, labels):
    run_dir = Path(run_dir)
    resolutions = {r["obs_id"]: r for r in _read_csv(run_dir / "resolutions.csv")}
    entities = _read_csv(run_dir / "entities.csv")
    claims = _read_csv(run_dir / "claims.csv")
    fields = _fields_from_bundle(run_dir)

    top1: dict[str, dict[str, str]] = defaultdict(dict)
    best: dict[tuple[str, str], float] = {}
    for c in claims:
        if c["kind"] != "field":
            continue
        key = (c["obs_id"], c["key"])
        weight = float(c["weight"])
        if weight > best.get(key, -1.0):
            best[key] = weight
            top1[c["obs_id"]][c["key"]] = c["value"]

    outcomes = []
    for label in labels:
        if label["key_type"] != "field":
            continue
        obs_id, field, expected = label["obs_id"], label["key"], label["value"]
        if obs_id not in resolutions or field not in fields:
            continue
        row = resolutions[obs_id]
        actual = row[field]
        provenance = row[f"{field}_provenance"]

        # §7.2.1 / §2.5: undecidable is excluded from denominators entirely.
        # (In practice no per-observation `actual` is ever literally
        # "undecidable" here: an observation that itself witnessed a
        # temporally-conflicting field reports its OWN reading, not the
        # entity's escape marker -- see obs_pipeline/fields.py's
        # observation_fields(). This branch stays as a defensive guard for
        # the general case rather than something exercised by this dataset.)
        if actual == UNDECIDABLE or label["status"] == "undecidable":
            correct = None
        else:
            correct = actual == expected

        outcomes.append({
            "obs_id": obs_id, "key_type": "field", "key": field,
            "expected": expected, "actual": actual, "correct": correct,
            "provenance": provenance,
            "top1_claim": top1.get(obs_id, {}).get(field, ""),
            "labeler_certainty": label["labeler_certainty"],
            "confidence": float(row["confidence"]),
            "stability": float(row["stability"]),
        })

    rows: list[dict] = []

    # --- Stage 1 & 2 (§3.1) -------------------------------------------------
    # Precision and recall take DIFFERENT denominators, and collapsing them
    # is the failure mode this block exists to avoid:
    #
    #   recall    = correctly extracted / everything the labels say was there
    #   precision = correctly extracted / everything we extracted directly
    #
    # §3.1's "direct rows only" instruction is about not CREDITING an
    # inherited (propagated) value as extraction the pipeline never
    # performed -- that governs the NUMERATOR (`hit`), computed from direct
    # rows only, in both metrics below. It does not govern recall's
    # DENOMINATOR: a label whose observation ended up `unknown` (nothing
    # extracted) or `propagated` (inherited, not witnessed by this payload)
    # is exactly the kind of row recall exists to count as a miss.
    # Excluding it there discards the misses recall is supposed to measure,
    # and because a `direct` row can never carry an escape/absent value, a
    # precision filter that also tried to drop escapes would drop nothing
    # -- which is what made precision and recall collapse into the same
    # number reported under two names.
    for field in fields:
        scored = [o for o in outcomes if o["key"] == field and o["correct"] is not None]
        direct = [o for o in scored if o["provenance"] == "direct"]
        hit = sum(1 for o in direct if o["correct"])
        if scored:
            rows.append(_row("extraction_recall", f"field:{field}",
                             hit / len(scored), len(scored)))
        if direct:
            rows.append(_row("extraction_precision", f"field:{field}:direct",
                             hit / len(direct), len(direct)))
        if scored:
            hit_top1 = sum(1 for o in scored if o["top1_claim"] == o["expected"])
            rows.append(_row("top1_claim_accuracy", f"field:{field}",
                             hit_top1 / len(scored), len(scored)))

    # --- Stage 4: propagated fields only (§3.1) ----------------------------
    for field in fields:
        prop = [o for o in outcomes
                if o["key"] == field and o["correct"] is not None
                and o["provenance"] == "propagated"]
        rows.append(_row("propagated_value_accuracy", f"field:{field}",
                         (sum(1 for o in prop if o["correct"]) / len(prop)) if prop else 0.0,
                         len(prop)))

    # --- Stage 3: pairwise, non-gating (§7.4) ------------------------------
    # §2.4: matches on the PARTITION, never on entity-id strings -- entity
    # ids are content-addressed and change when membership changes. String
    # equality is used below only to test co-membership (an equivalence
    # relation), never to compare a predicted id against a label-supplied
    # one -- labels.csv carries no entity ids at all, only same_device pairs.
    truth_pairs = {tuple(sorted([l["obs_id"], l["value"]]))
                   for l in labels if l.get("key") == "same_device"}
    entity_of = {obs_id: row["entity_id"] for obs_id, row in resolutions.items()}
    predicted_pairs = {
        tuple(sorted([a, b]))
        for a, b in combinations(sorted(entity_of), 2)
        if entity_of[a] == entity_of[b]
    }
    tp = len(truth_pairs & predicted_pairs)
    false_merges = predicted_pairs - truth_pairs   # predicted-same, truth-different
    false_splits = truth_pairs - predicted_pairs   # truth-same, predicted-different

    rows.append(_row("pairwise_precision", "global",
                     tp / max(len(predicted_pairs), 1), len(truth_pairs)))
    rows.append(_row("pairwise_recall", "global",
                     tp / max(len(truth_pairs), 1), len(truth_pairs)))
    rows.append(_row("false_merge_count", "global", len(false_merges),
                     len(predicted_pairs)))
    rows.append(_row("false_split_count", "global", len(false_splits),
                     len(truth_pairs)))

    # --- undecidable: a coverage statistic about the SOURCES, not a rules
    # failure (§7.2.1) -- reported separately rather than folded into the
    # precision/recall denominators above. This is entity-level, not
    # per-observation: `undecidable` only ever arises as an ENTITY's
    # resolved value when two members' direct readings temporally disagree
    # (field_resolution.yaml's per_field conflict_policy); each individual
    # observation still reports what it actually witnessed. So the harness
    # reads entities.csv directly for this one, rather than the per-
    # observation `outcomes` used everywhere else.
    total_cells = len(entities) * len(fields)
    undecidable_cells = sum(1 for e in entities for f in fields
                            if e[f] == UNDECIDABLE)
    rows.append(_row("undecidable_rate", "global",
                     undecidable_cells / max(total_cells, 1), total_cells))

    # --- calibration: is confidence HONEST, not high (§8.4) ----------------
    buckets: dict[str, list] = defaultdict(list)
    for o in outcomes:
        if o["correct"] is None:
            continue
        buckets[f"{min(int(o['confidence'] * 10) / 10, 0.9):.1f}"].append(o["correct"])
    for b, results in sorted(buckets.items()):
        observed = sum(1 for r in results if r) / len(results)
        rows.append(_row("confidence_calibration_error", f"bucket:{b}",
                         abs(observed - float(b)), len(results)))

    # §8.4 -- stability validation. Needs no partition: it asks the honest
    # question directly rather than defining a stratum (§7.2.3).
    stab_buckets: dict[str, list] = defaultdict(list)
    for o in outcomes:
        if o["correct"] is None:
            continue
        stab_buckets[f"{min(int(o['stability'] * 10) / 10, 0.9):.1f}"].append(
            o["correct"])
    for b, results in sorted(stab_buckets.items()):
        rows.append(_row("accuracy_by_stability", f"bucket:{b}",
                         sum(1 for r in results if r) / len(results),
                         len(results)))

    # The high-confidence / low-stability quadrant is where stability earns
    # its keep: these should be materially less accurate than
    # high-confidence / high-stability results, and if they aren't,
    # confidence alone was sufficient after all (§8.4).
    for label, predicate in (
        ("high_conf_high_stab", lambda o: o["confidence"] >= 0.7 and o["stability"] >= 0.7),
        ("high_conf_low_stab", lambda o: o["confidence"] >= 0.7 and o["stability"] < 0.7),
    ):
        subset = [o for o in outcomes if o["correct"] is not None and predicate(o)]
        rows.append(_row("accuracy_by_stability", f"quadrant:{label}",
                         (sum(1 for o in subset if o["correct"]) / len(subset))
                         if subset else 0.0, len(subset)))

    return rows, outcomes


class LabelsMovedError(Exception):
    """Both rules and labels changed; a single-axis diff would misattribute
    the cause (§7.6)."""


def four_bucket_diff(before, after, *, before_labels_hash=None,
                     after_labels_hash=None) -> dict[str, list[dict]]:
    """§7.6 -- aggregate metrics are insufficient on their own.

    A rule change can improve overall precision/recall while silently breaking
    specific cases that previously resolved correctly. This diffs PER-LABEL
    outcomes, not totals.
    """
    if (before_labels_hash is not None and after_labels_hash is not None
            and before_labels_hash != after_labels_hash):
        raise LabelsMovedError(
            "labels_hash differs between runs; run two passes instead -- "
            "rules-held-constant to isolate the label delta, and "
            "labels-held-constant to isolate the rule delta (§7.6)"
        )

    def key(o):
        return (o["obs_id"], o["key_type"], o["key"])

    b = {key(o): o for o in before if o["correct"] is not None}
    a = {key(o): o for o in after if o["correct"] is not None}

    out: dict[str, list[dict]] = {
        "fixed": [], "broken": [], "stable_correct": [], "stable_incorrect": [],
    }
    for k in sorted(set(b) & set(a)):
        was, now = b[k]["correct"], a[k]["correct"]
        bucket = ("stable_correct" if was and now else
                  "stable_incorrect" if not was and not now else
                  "fixed" if now else "broken")
        out[bucket].append({**a[k], "before_value": b[k]["actual"]})
    return out


def two_pass_diff(*, baseline_outcomes, rules_held_outcomes,
                  labels_held_outcomes, baseline_labels_hash,
                  current_labels_hash) -> dict[str, dict | None]:
    """§7.6 -- label mutation must not masquerade as rule regression.

    Adjudication (§7.2.2) mutates labels. If a label flips from Dahua to
    Hikvision between eval runs, the affected case lands in `broken` and reads
    as a rule regression when in fact the ground truth moved. When both rules
    and labels changed, run two passes so the two causes stay separable.

    Stickiness (§7.7) is what makes this load-bearing rather than theoretical:
    labels mostly hold still but occasionally move, which is exactly the
    mutation pattern that would otherwise contaminate the regression signal.
    """
    labels_moved = baseline_labels_hash != current_labels_hash

    label_delta = None
    if labels_moved:
        if rules_held_outcomes is None:
            raise LabelsMovedError(
                "labels_hash changed but no rules-held-constant pass was "
                "supplied; the label delta cannot be isolated (§7.6)"
            )
        # rules_held_outcomes is scored under the SAME (old) rules as
        # baseline_outcomes, with only the labels moved -- so diffing it
        # against baseline isolates the label delta with the rule axis held
        # fixed. Every flip here is attributable to the labels.
        label_delta = four_bucket_diff(baseline_outcomes, rules_held_outcomes)

    # labels_held_outcomes is scored under the SAME (old) labels as
    # baseline_outcomes, with only the rules moved -- so diffing it against
    # baseline isolates the rule delta with the label axis held fixed. Every
    # flip here is attributable to the rules. Always compare against
    # baseline_outcomes, not against rules_held_outcomes: the latter has
    # already moved along the label axis, so diffing the two moved passes
    # against each other would not isolate either cause.
    rule_delta = four_bucket_diff(baseline_outcomes, labels_held_outcomes)

    return {"label_delta": label_delta, "rule_delta": rule_delta}


def propose_bump(diff, *, before_keys, after_keys, before_vocab, after_vocab) -> str:
    """§7.6 -- derive the version bump from MEASURED BEHAVIOR rather than
    leaving it to whoever wrote the commit. Advisory to the human, paired with
    the load-time hash enforcement in §6.2."""
    if set(before_keys) != set(after_keys):
        return "major"                      # entity pool / membership consumers break
    if set(before_vocab) - set(after_vocab):
        return "major"                      # a value consumers saw can no longer be emitted
    if set(after_vocab) - set(before_vocab):
        return "minor"                      # additive
    if diff["fixed"] or diff["broken"]:
        return "minor"                      # recalibration; schema intact
    return "patch"                          # behavior-neutral


def write_regressions(path, diff, changed_rule_files) -> None:
    """§7.6 -- regressions are LOGGED, not blocked. Merging is not gated on
    this; the log is advisory. Surfaced in the scorecard and REPORT.md rather
    than buried in a side file someone has to know to open."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["obs_id", "key_type", "key", "before", "after",
                    "changed_rule_files"])
        for o in diff["broken"]:
            w.writerow([o["obs_id"], o["key_type"], o["key"],
                        o.get("before_value", ""), o["actual"],
                        "|".join(sorted(changed_rule_files))])


def evaluate(observations_path, rules_dir, labels_path, out_root,
             runs_root="runs", baseline_outcomes=None,
             baseline_labels_hash=None, baseline_run_id=None,
             changed_rule_files=()) -> Path:
    # Validate BEFORE running or writing anything. Two reasons:
    #
    # A baseline with no `baseline_labels_hash` cannot be compared safely --
    # the guard in four_bucket_diff only fires when BOTH hashes are known, so
    # omitting one silently produces the very misattribution this refusal
    # exists to prevent: a moved label read as a rule regression. Forgetting
    # one keyword argument is exactly the bookkeeping lapse §7.6 describes.
    #
    # And refusing only after the manifest, metrics and outcomes are on disk
    # leaves a half-written eval directory with no regressions.csv, which
    # downstream readers would have to special-case.
    current_labels_hash = labels_hash(labels_path)
    if baseline_outcomes is not None and baseline_labels_hash is None:
        raise LabelsMovedError(
            "baseline_outcomes was supplied without baseline_labels_hash, so "
            "there is no way to prove the label set did not move between the "
            "two runs. Pass the baseline's labels_hash, or omit the baseline."
        )
    if (baseline_labels_hash is not None
            and baseline_labels_hash != current_labels_hash):
        raise LabelsMovedError(
            f"labels_hash differs between runs ({baseline_labels_hash} != "
            f"{current_labels_hash}); run two passes instead -- "
            f"rules-held-constant to isolate the label delta, and "
            f"labels-held-constant to isolate the rule delta (§7.6)"
        )

    run_dir = run_pipeline(observations_path, rules_dir, runs_root)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    labels = load_labels(labels_path)

    rows, outcomes = score_against_labels(run_dir, labels)
    registry = load_registry("metrics.yaml")
    rows = [r for r in rows if r["metric"] in registry]

    eval_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = Path(out_root) / eval_id
    out.mkdir(parents=True, exist_ok=True)

    eval_manifest = {
        "eval_id": eval_id,
        "run_id": manifest["run_id"],
        "rules_rollup": manifest["rules_rollup"],
        "labels_version": (Path(labels_path).parent / "VERSION").read_text(
            encoding="utf-8").strip(),
        "labels_hash": labels_hash(labels_path),
        "baseline_run_id": None,
        "baseline_labels_hash": None,
    }
    (out / "eval_manifest.json").write_text(
        json.dumps(eval_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with open(out / "metrics.jsonl", "w", encoding="utf-8") as fh:
        for r in sorted(rows, key=lambda r: (r["metric"], r["scope"])):
            fh.write(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n")

    (out / "outcomes.json").write_text(
        json.dumps(outcomes, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if baseline_outcomes is not None:
        diff = four_bucket_diff(baseline_outcomes, outcomes,
                                before_labels_hash=baseline_labels_hash,
                                after_labels_hash=current_labels_hash)
        eval_manifest["baseline_run_id"] = baseline_run_id
        eval_manifest["baseline_labels_hash"] = baseline_labels_hash
        (out / "eval_manifest.json").write_text(
            json.dumps(eval_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        (out / "four_bucket.json").write_text(
            json.dumps({k: len(v) for k, v in diff.items()}, indent=2,
                       sort_keys=True) + "\n", encoding="utf-8")
    else:
        diff = {"fixed": [], "broken": [], "stable_correct": [],
                "stable_incorrect": []}
    write_regressions(out / "regressions.csv", diff, changed_rule_files)

    return out


if __name__ == "__main__":
    print(evaluate(
        sys.argv[1] if len(sys.argv) > 1 else "obs-data/observations.csv",
        sys.argv[2] if len(sys.argv) > 2 else "rules",
        sys.argv[3] if len(sys.argv) > 3 else "labels/labels.csv",
        sys.argv[4] if len(sys.argv) > 4 else "evals",
    ))
