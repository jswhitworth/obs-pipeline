#!/usr/bin/env python3
"""§5 reporting and narration (free-thinking-llm-options.md, item #6).

Offline. Reads a WRITTEN run bundle (and optionally an eval dir) and writes
NARRATIVE.md beside REPORT.md:

- **Low-confidence explanation** -- for entities below the confidence
  threshold, a one-line human diagnosis of WHY, derived from their claims.
- **Run-diff narration** -- when an eval dir is supplied, the story behind
  the four-bucket counts and regression rows.
- **Anomaly spotting** -- inconsistencies metrics don't encode (model on
  the wrong device class, firmware implausible for the line, OUI/vendor
  mismatch).

NARRATIVE.md is a derived, ADVISORY artifact with the same standing as
REPORT.md: regenerable, never hand-edited, and nothing may depend on
parsing it. It is written by this offline tool, never by the pipeline --
report.py stays model-free, so the runtime path keeps zero LLM footprint.
Every narrative is stamped with the model id and run_id it narrates.
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

import llm_client
from llm_client import DEFAULT_MODEL, call_model, parse_response

_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["run_summary", "entity_notes", "anomalies"],
    "properties": {
        "run_summary": {"type": "string"},
        "entity_notes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["entity_id", "note"],
                "properties": {"entity_id": {"type": "string"},
                               "note": {"type": "string"}},
            },
        },
        "anomalies": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["subject", "note"],
                "properties": {"subject": {"type": "string"},
                               "note": {"type": "string"}},
            },
        },
    },
}


def _read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def gather(run_dir, eval_dir=None, threshold=0.6) -> dict:
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text(
        encoding="utf-8"))
    entities = _read_csv(run_dir / "entities.csv")
    resolutions = _read_csv(run_dir / "resolutions.csv")
    claims = _read_csv(run_dir / "claims.csv")

    members = defaultdict(list)
    for r in resolutions:
        members[r["entity_id"]].append(r["obs_id"])
    claims_by_obs = defaultdict(list)
    for c in claims:
        claims_by_obs[c["obs_id"]].append(c)

    low = []
    for e in sorted(entities, key=lambda e: float(e["confidence"])):
        if float(e["confidence"]) >= threshold:
            continue
        eid = e["entity_id"]
        low.append({
            "entity": e,
            "members": sorted(members.get(eid, [])),
            "claims": sorted(
                (c for oid in members.get(eid, [])
                 for c in claims_by_obs.get(oid, [])),
                key=lambda c: (c["obs_id"], c["kind"], c["key"], c["value"])),
        })

    out = {"manifest": manifest, "entities": entities, "low": low,
           "eval": None}
    if eval_dir:
        eval_dir = Path(eval_dir)
        out["eval"] = {
            "manifest": json.loads((eval_dir / "eval_manifest.json")
                                   .read_text(encoding="utf-8")),
            "four_bucket": (json.loads((eval_dir / "four_bucket.json")
                                       .read_text(encoding="utf-8"))
                            if (eval_dir / "four_bucket.json").exists()
                            else None),
            "regressions": _read_csv(eval_dir / "regressions.csv"),
        }
    return out


def build_prompt(gathered: dict) -> str:
    lines = [
        "You are narrating a device-fingerprinting pipeline run for a",
        "human reviewer. Be specific and evidence-based; every note must be",
        "checkable against the data below. Produce:",
        "- run_summary: a short paragraph a reviewer actually wants -- what",
        "  resolved cleanly, where the weakness is concentrated, and (if an",
        "  eval diff is present) the story behind the fixed/broken counts.",
        "- entity_notes: for each low-confidence entity, ONE line",
        "  diagnosing why confidence is low (e.g. which field's evidence is",
        "  thin or conflicting), far more actionable than the bare number.",
        "- anomalies: inconsistencies the metrics do not encode -- a camera",
        "  model with an NVR port profile, firmware implausible for that",
        "  model line, MAC OUI inconsistent with the resolved vendor.",
        "  Subject is the entity_id or obs_id concerned. An empty list is a",
        "  valid answer.",
        "",
        "Resolved entities (all):",
    ]
    for e in sorted(gathered["entities"], key=lambda e: e["entity_id"]):
        lines.append(
            f"- {e['entity_id']}: vendor={e['vendor']!r} model={e['model']!r} "
            f"firmware={e['firmware']!r} device_type={e['device_type']!r} "
            f"confidence={e['confidence']} stability={e['stability']}")
    lines.append("")
    lines.append("Low-confidence entities, with member claims:")
    if not gathered["low"]:
        lines.append("(none below threshold)")
    for item in gathered["low"]:
        e = item["entity"]
        lines.append(f"- {e['entity_id']} confidence={e['confidence']} "
                     f"members={item['members']}")
        for c in item["claims"]:
            lines.append(f"    {c['obs_id']} {c['kind']}:{c['key']} = "
                         f"{c['value']!r} (weight={c['weight']}, "
                         f"sources={c['sources']})")
    ev = gathered["eval"]
    if ev:
        lines += ["", "Eval overlay:",
                  f"four_bucket={json.dumps(ev['four_bucket'], sort_keys=True)}"]
        for r in ev["regressions"]:
            lines.append(f"regression: {r}")
    return "\n".join(lines)


def narrate(run_dir, model=DEFAULT_MODEL, *, eval_dir=None, threshold=0.6,
            out_path=None, cache_dir="llm_cache", transport=None,
            sink=None) -> Path:
    run_dir = Path(run_dir)
    gathered = gather(run_dir, eval_dir, threshold)
    prompt = build_prompt(gathered)
    body = llm_client.request_body(model, prompt, _RESPONSE_SCHEMA)
    resp, key, cached, elapsed_ms = call_model(body, cache_dir, transport)
    parsed = parse_response(resp)

    known = {e["entity_id"] for e in gathered["entities"]}
    notes = [n for n in parsed.get("entity_notes", [])
             if n["entity_id"] in known]

    run_id = gathered["manifest"]["run_id"]
    trace_url = None
    if sink:
        trace_url = sink(
            tool="narrate", model=model, prompt=prompt,
            output_text=json.dumps(parsed, indent=2, sort_keys=True),
            usage=resp.get("usage"), cached=cached, cache_key=key,
            elapsed_ms=elapsed_ms,
            metadata={"run_id": run_id, "threshold": threshold,
                      "eval_dir": str(eval_dir) if eval_dir else None},
            spans=[("gather", {"entities": len(gathered["entities"]),
                               "low_confidence": len(gathered["low"])})],
            scores=[("entity_notes", len(notes)),
                    ("anomalies", len(parsed.get("anomalies", [])))],
            session_id=run_id)

    lines = [
        "# NARRATIVE (advisory, model-derived)",
        "",
        f"- run_id: `{run_id}`",
        f"- model: `{model}` (origin: llm_proposed)",
        f"- cache_key: `{key}`",
        *( [f"- trace: {trace_url}"] if trace_url else [] ),
        "",
        "Derived render, regenerable via narrate.py; never hand-edit, and",
        "nothing may depend on parsing it (same standing as REPORT.md).",
        "",
        "## Run summary",
        "",
        parsed.get("run_summary", "").strip(),
        "",
        "## Low-confidence entities",
        "",
    ]
    if notes:
        lines += [f"- **{n['entity_id']}** — {n['note']}"
                  for n in sorted(notes, key=lambda n: n["entity_id"])]
    else:
        lines.append("(none)")
    lines += ["", "## Anomalies", ""]
    anomalies = parsed.get("anomalies", [])
    if anomalies:
        lines += [f"- **{a['subject']}** — {a['note']}"
                  for a in sorted(anomalies, key=lambda a: a["subject"])]
    else:
        lines.append("(none flagged)")

    out_path = Path(out_path) if out_path else run_dir / "NARRATIVE.md"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def main(argv: list[str]) -> int:
    opts = {"--model": DEFAULT_MODEL, "--eval": None, "--threshold": "0.6",
            "--out": None, "--cache-dir": "llm_cache"}
    positional = []
    i = 0
    while i < len(argv):
        if argv[i] in opts:
            opts[argv[i]] = argv[i + 1]
            i += 2
        else:
            positional.append(argv[i])
            i += 1
    if len(positional) != 1:
        print("usage: python3 narrate.py <run_dir> [--model ID] "
              "[--eval evals/<id>] [--threshold 0.6] [--out PATH] "
              "[--cache-dir llm_cache]", file=sys.stderr)
        return 2
    import langfuse_sink
    print(narrate(positional[0], opts["--model"], eval_dir=opts["--eval"],
                  threshold=float(opts["--threshold"]), out_path=opts["--out"],
                  cache_dir=opts["--cache-dir"],
                  sink=(langfuse_sink.emit_tool_trace
                        if langfuse_sink.enabled() else None)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
