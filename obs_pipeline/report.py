# obs_pipeline/report.py
"""REPORT.md -- a derived render, never a source of truth (design doc §3).

This inverts the usual arrangement deliberately: anything worth trending
should be queryable without re-parsing prose. This module reads the bundle's
structured files and makes no decisions of its own, so it emits no trace steps.
"""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

from obs_pipeline.fields import ABSENT_VALUES, UNDECIDABLE

FIELDS = ["vendor", "model", "device_type", "firmware"]


def _read(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_report(run_dir) -> Path:
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    entities = _read(run_dir / "entities.csv")
    membership = _read(run_dir / "membership.csv")
    claims = _read(run_dir / "claims.csv")

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

    for f in FIELDS:
        values = [e[f] for e in entities]
        unknown = sum(1 for v in values if v in ABSENT_VALUES)
        undecidable = sum(1 for v in values if v == UNDECIDABLE)
        known = len(values) - unknown - undecidable
        confs = [float(e[f"{f}_confidence"]) for e in entities]
        mean = sum(confs) / len(confs) if confs else 0.0
        lines.append(f"| `{f}` | {known} | {unknown} | {undecidable} | {mean:.2f} |")

    rejected = [c["value"] for c in claims if c["in_vocab"] == "False"]
    lines += [
        "",
        "## Vocabulary rejects",
        "",
        "*Ranked expansion queue (§6.3). A frequently-rejected value is either "
        "a missing vocabulary entry or a missing alias.*",
        "",
    ]
    if rejected:
        lines += ["| Value | Count |", "|---|---|"]
        # Sort by frequency, then by value. most_common() leaves the twelve
        # count-1 rows ordered by whichever obs_id happened to sort first,
        # which is deterministic but not scannable for a triage queue.
        ranked = sorted(Counter(rejected).items(), key=lambda kv: (-kv[1], kv[0]))
        lines += [f"| `{v}` | {n} |" for v, n in ranked]
    else:
        lines.append("*None.*")

    out = run_dir / "REPORT.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
