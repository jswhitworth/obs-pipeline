# obs_pipeline/report.py
"""REPORT.md -- a derived render, never a source of truth (design doc §3).

This inverts the usual arrangement deliberately: anything worth trending
should be queryable without re-parsing prose. This module reads the bundle's
structured files and makes no decisions of its own, so it emits no trace steps.
"""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

from obs_pipeline.fields import UNDECIDABLE


def _read(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _cell(value) -> str:
    """Escape `|` so a value can never masquerade as a markdown table-cell
    delimiter. `changed_rule_files` is pipe-joined by eval.py, so a
    multi-file regression is the ordinary case, not an edge case -- and
    `before`/`after` carry extracted payload text that is safe today only
    because the extraction regexes happen to exclude `|`, which is a rules
    detail, not a guarantee. Applied to every cell in the regressions row,
    not just the column known to need it."""
    return str(value).replace("|", "\\|")


# Columns entities.csv writes around the per-field ones (bundle.py). Not
# rules-declared -- this is the bundle's own fixed schema, not a vocabulary.
_NON_FIELD_COLUMNS = {"run_id", "derivation_step", "entity_id", "confidence", "stability"}


def _field_names(entities) -> list[str]:
    """The field list, read from entities.csv's own header (design doc §3)
    rather than a hardcoded copy of claims.yaml#fields. report.py renders a
    written bundle, so it must follow that bundle's actual schema: if a field
    is added or dropped from the rules, entities.csv's columns move with it,
    and this should move too rather than silently ignoring the new column or
    KeyError-ing on the missing one.

    Deliberately NOT sorted: bundle.py writes the header in claims.yaml's
    declared order (vendor, model, firmware, device_type -- tracking design
    doc §3's most-identifying-field-first ordering), and this reads that
    order back as-is so the "Field resolution" table matches entities.csv
    column-for-column rather than presenting its own alphabetized view."""
    if not entities:
        return []
    return [c for c in entities[0]
            if c not in _NON_FIELD_COLUMNS and not c.endswith("_confidence")]


def write_report(run_dir, eval_dir=None) -> Path:
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    entities = _read(run_dir / "entities.csv")
    membership = _read(run_dir / "membership.csv")
    claims = _read(run_dir / "claims.csv")
    fields = _field_names(entities)
    # §2.5 -- the escape/absence vocabulary comes from claims.yaml at rule
    # load time; report.py has no RuleSet, so bundle.py writes it into the
    # manifest once, at the point that does have the rules loaded.
    absent_values = frozenset(manifest.get("absent_values", []))

    sizes = Counter(m["entity_id"] for m in membership)
    singletons = sum(1 for c in sizes.values() if c == 1)
    contested = sum(1 for m in membership if m["basis_agreement"] == "False")

    lines = [
        "# Run Report",
        "",
        "*Generated from the run bundle. Never hand-edit — regenerate with "
        "`python3 -c \"from obs_pipeline.report import write_report; "
        "write_report('<run_dir>')\"`.*",
        "",
        "*The run directory is not embedded above on purpose: it would make "
        "this file differ after a bundle is copied or archived, for a reason "
        "that has nothing to do with the run.*",
        "",
        "## Run provenance",
        "",
        f"- `run_id`: `{manifest['run_id']}`",
        f"- `rules_version`: **{manifest['rules_version']}**",
        f"- `rules_rollup`: `{manifest['rules_rollup']}`",
        f"- `input_hash`: `{manifest['input_hash']}`",
        f"- `version_verified`: **{manifest['version_verified']}**",
        f"- `engine_commit`: `{manifest['engine_commit']}`",
        "",
        "## Clustering shape",
        "",
        f"- Observations: **{len(membership)}**",
        f"- Entities: **{len(entities)}**",
        f"- Singletons: **{singletons}** "
        f"({singletons / max(len(entities), 1):.0%})",
        f"- Largest cluster: **{max(sizes.values()) if sizes else 0}**",
        f"- Observations with cross-basis contradiction: **{contested}**",
        "",
        "## Field resolution",
        "",
        "| Field | Known | Unknown | Undecidable | Mean confidence |",
        "|---|---|---|---|---|",
    ]

    for f in fields:
        values = [e[f] for e in entities]
        unknown = sum(1 for v in values if v in absent_values)
        undecidable = sum(1 for v in values if v == UNDECIDABLE)
        known = len(values) - unknown - undecidable
        confs = [float(e[f"{f}_confidence"]) for e in entities]
        mean = sum(confs) / len(confs) if confs else 0.0
        lines.append(f"| `{f}` | {known} | {unknown} | {undecidable} | {mean:.2f} |")

    # Keyed by (field, value), not value alone: the queue is a work list, and
    # the work is "add an entry to *this* column of canonical_vocab.csv".
    # A bare surface string does not say which vocabulary is short, and the
    # same string can be rejected against two different fields.
    # Witnesses are carried alongside the count: a rejected value is only
    # triageable against the raw observation that produced it, and without the
    # obs_ids the reader has to go re-derive the join from claims.csv by hand.
    # A set, not a list: claim construction already merges the witnesses of one
    # (obs_id, key, value) into a single claim, so the count IS the number of
    # distinct observations. Deriving both from one set keeps them from drifting
    # apart if that ever stops being true.
    rejected: dict[tuple[str, str], set[str]] = defaultdict(set)
    for c in claims:
        if c["in_vocab"] == "False":
            rejected[(c["key"], c["value"])].add(c["obs_id"])
    lines += [
        "",
        "## Vocabulary rejects",
        "",
        "*Ranked expansion queue (§6.3). A frequently-rejected value is either "
        "a missing vocabulary entry or a missing alias, in the named field's "
        "column of `canonical_vocab.csv`. Observations are the witnesses to "
        "check before extending the vocabulary.*",
        "",
    ]
    if rejected:
        lines += ["| Field | Value | Count | Observations |", "|---|---|---|---|"]
        # Sort by frequency, then by field and value. Counter.most_common()
        # leaves the twelve count-1 rows ordered by whichever obs_id happened
        # to sort first, which is deterministic but not scannable for a triage
        # queue. Witnesses sort too -- input row order must not reach output.
        ranked = sorted(rejected.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        lines += [
            # _cell for the same reason as the regressions row: the value is
            # raw banner/sysDescr payload, pipe-free only by accident of the
            # current extraction regexes.
            f"| `{f}` | `{_cell(v)}` | {len(obs)} | "
            f"{', '.join('`' + o + '`' for o in sorted(obs))} |"
            for (f, v), obs in ranked
        ]
    else:
        lines.append("*None.*")

    # §7.6 -- surface, don't bury. The fixed/broken counts and the full broken
    # list belong here, not only in a side file someone has to know to open.
    # This reads a written eval bundle, which is still rendering (not
    # deciding): the fixed/broken/stable buckets and the regression rows are
    # already-computed facts in eval_dir's structured files, not something
    # report.py derives.
    if eval_dir is not None:
        eval_dir = Path(eval_dir)
        lines += ["", "## Regressions since the baseline rule state", ""]
        fb_path = eval_dir / "four_bucket.json"
        if fb_path.exists():
            fb = json.loads(fb_path.read_text(encoding="utf-8"))
            lines += [
                "| Bucket | Count |", "|---|---|",
                f"| `fixed` | {fb.get('fixed', 0)} |",
                f"| **`broken`** | **{fb.get('broken', 0)}** |",
                f"| `stable_correct` | {fb.get('stable_correct', 0)} |",
                f"| `stable_incorrect` | {fb.get('stable_incorrect', 0)} |",
                "",
            ]
        else:
            lines += ["*No baseline was supplied for this eval, so there is "
                      "no fixed/broken comparison to show.*", ""]
        reg_path = eval_dir / "regressions.csv"
        if reg_path.exists():
            regressions = _read(reg_path)
            if regressions:
                lines += [
                    "*Advisory, not blocking (§7.6): merging is not gated on "
                    "this list. It shows only THIS run's regressions -- a "
                    "label that flips `broken` and is never fixed will not "
                    "stay visible on its own across subsequent runs unless "
                    "something re-surfaces it here.*",
                    "",
                    "| obs_id | key | before | after | changed rules |",
                    "|---|---|---|---|---|",
                ]
                lines += [
                    f"| `{_cell(r['obs_id'])}` | `{_cell(r['key'])}` | "
                    f"`{_cell(r['before'])}` | `{_cell(r['after'])}` | "
                    f"`{_cell(r['changed_rule_files'])}` |"
                    for r in regressions
                ]
            else:
                lines.append("*No regressions.*")

    out = run_dir / "REPORT.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
