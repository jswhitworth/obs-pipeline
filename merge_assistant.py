#!/usr/bin/env python3
"""§3 merge adjudication assistant (free-thinking-llm-options.md, item #5).

Offline. The deterministic clustering handles the easy cases; the residue
is the `merge_refused` trace steps -- pairs whose link weight fell below
threshold, and observations whose bases contradicted each other. For each,
the LLM sees both sides' full claim sets and raw evidence and reasons like
the human who would otherwise review it.

The output is ADVISORY: merge_verdicts.csv (verdict, rationale, confidence)
queued for a human to accept in bulk. Nothing here writes labels or touches
entity resolution -- invariant #6's split applies: this tool may READ
pipeline output to select cases, but turning a verdict into a same_device
label goes through the existing adjudication path, where presentation is
blind. Deliberately, verdicts use the label-side vocabulary (same_device /
different_device / unclear), not link_basis keys: a verdict is a pairwise
human-style judgement, not a clustering signal.
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
    "required": ["verdicts"],
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["case_id", "verdict", "rationale", "confidence"],
                "properties": {
                    "case_id": {"type": "string"},
                    "verdict": {"type": "string",
                                "enum": ["same_device", "different_device",
                                         "unclear"]},
                    "rationale": {"type": "string"},
                    "confidence": {"type": "string",
                                   "enum": ["low", "medium", "high"]},
                },
            },
        },
    },
}

_CSV_COLUMNS = ("case_id", "kind", "obs_ids", "verdict", "rationale",
                "llm_confidence", "model", "origin")


def collect_cases(run_dir) -> list[dict]:
    """merge_refused steps from the written trace, deduplicated. Two kinds:
    below_threshold (a rejected pairwise edge) and cross_basis_conflict (one
    observation whose bases disagreed about its cluster)."""
    cases: dict[str, dict] = {}
    with open(Path(run_dir) / "trace.jsonl", encoding="utf-8") as fh:
        for line in fh:
            step = json.loads(line)
            if step.get("op") != "merge_refused":
                continue
            obs_ids = sorted(ref[len("obs:"):] for ref in step.get("inputs", [])
                             if ref.startswith("obs:"))
            case_id = "|".join(obs_ids)
            cases.setdefault(case_id, {
                "case_id": case_id,
                "kind": step.get("reason", "unknown"),
                "obs_ids": obs_ids,
                "detail": step.get("detail", {}),
            })
    return [cases[k] for k in sorted(cases)]


def build_prompt(cases: list[dict], claims_by_obs: dict[str, list[dict]],
                 obs_by_id: dict[str, dict]) -> str:
    lines = [
        "You are adjudicating borderline entity-resolution decisions for a",
        "device-fingerprinting pipeline. Each case below was REFUSED by the",
        "deterministic clusterer: either a candidate pair's link weight fell",
        "below threshold, or one observation's identity bases contradicted",
        "each other. You get every claim each observation produced plus the",
        "raw evidence. Reason like a network engineer: serial formats, MAC",
        "OUIs, model suffixes, rebadged OEM hardware, NVRs fronting cameras.",
        "",
        "Verdicts: same_device (the observations describe one physical",
        "device), different_device, or unclear when the evidence genuinely",
        "cannot decide. Your verdicts are queued for human review -- give a",
        "rationale a reviewer can check against the evidence, and use",
        "`unclear` freely rather than guessing.",
        "",
        "Cases:",
    ]
    for case in cases:
        lines.append(f"- case_id={case['case_id']} kind={case['kind']} "
                     f"detail={json.dumps(case['detail'], sort_keys=True)}")
        for obs_id in case["obs_ids"]:
            o = obs_by_id.get(obs_id, {})
            lines.append(
                f"    {obs_id}: source={o.get('source', '?')} "
                f"payload={o.get('raw_payload', '')!r} mac={o.get('mac', '')} "
                f"hostname={o.get('hostname', '')} "
                f"ports={o.get('open_ports', '')}")
            for c in claims_by_obs.get(obs_id, []):
                lines.append(f"        claim {c['kind']}:{c['key']} = "
                             f"{c['value']!r} (weight={c['weight']})")
    return "\n".join(lines)


def validate_verdict(v: dict, case_ids: set[str],
                     seen: set[str]) -> list[str]:
    reasons = []
    if v.get("case_id") not in case_ids:
        reasons.append(f"case_id {v.get('case_id')!r} is not a collected "
                       f"merge_refused case")
    if v.get("case_id") in seen:
        reasons.append(f"duplicate verdict for {v.get('case_id')!r}")
    return reasons


def assist(run_dir, model=DEFAULT_MODEL, *,
           observations_path="obs-data/observations.csv",
           out_root="proposals/merges", cache_dir="llm_cache",
           transport=None, sink=None) -> Path:
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text(
        encoding="utf-8"))
    cases = collect_cases(run_dir)

    out = Path(out_root) / model
    out.mkdir(parents=True, exist_ok=True)

    proposal = {
        "origin": "llm_proposed",
        "model": model,
        "run_id": manifest["run_id"],
        "cases": cases,
        "accepted": [], "rejected": [],
        "prompt_hash": None, "cache_key": None, "cached": None, "usage": None,
    }

    if cases:
        wanted = {oid for c in cases for oid in c["obs_ids"]}
        claims_by_obs: dict[str, list[dict]] = defaultdict(list)
        with open(run_dir / "claims.csv", newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row["obs_id"] in wanted:
                    claims_by_obs[row["obs_id"]].append(row)
        for rows in claims_by_obs.values():
            rows.sort(key=lambda r: (r["kind"], r["key"], r["value"]))
        with open(observations_path, newline="", encoding="utf-8") as fh:
            obs_by_id = {r["obs_id"]: r for r in csv.DictReader(fh)
                         if r["obs_id"] in wanted}

        prompt = build_prompt(cases, claims_by_obs, obs_by_id)
        body = llm_client.request_body(model, prompt, _RESPONSE_SCHEMA)
        resp, key, cached, elapsed_ms = call_model(body, cache_dir, transport)
        parsed = parse_response(resp)
        proposal.update(prompt_hash=llm_client.sha256(prompt.encode("utf-8")),
                        cache_key=key, cached=cached,
                        usage=resp.get("usage"))
        case_ids = {c["case_id"] for c in cases}
        seen: set[str] = set()
        for v in parsed.get("verdicts", []):
            reasons = validate_verdict(v, case_ids, seen)
            if reasons:
                proposal["rejected"].append({"verdict": v, "reasons": reasons})
                continue
            seen.add(v["case_id"])
            proposal["accepted"].append(v)

    if sink and proposal["cache_key"]:
        proposal["trace_url"] = sink(
            tool="merge_assistant", model=model, prompt=prompt,
            output_text=json.dumps(parsed, indent=2, sort_keys=True),
            usage=proposal["usage"], cached=cached,
            cache_key=proposal["cache_key"], elapsed_ms=elapsed_ms,
            metadata={"run_id": manifest["run_id"]},
            spans=[("collect_cases", {"cases": len(cases)}),
                   ("validate", {"accepted": len(proposal["accepted"]),
                                 "rejected": len(proposal["rejected"])})],
            scores=[("verdicts", len(proposal["accepted"]))],
            session_id=manifest["run_id"])

    (out / "proposal.json").write_text(
        json.dumps(proposal, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")

    kind_of = {c["case_id"]: c["kind"] for c in cases}
    obs_of = {c["case_id"]: c["obs_ids"] for c in cases}
    with open(out / "merge_verdicts.csv", "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(_CSV_COLUMNS)
        for v in sorted(proposal["accepted"], key=lambda v: v["case_id"]):
            w.writerow([v["case_id"], kind_of[v["case_id"]],
                        "|".join(obs_of[v["case_id"]]), v["verdict"],
                        v["rationale"], v["confidence"], model,
                        "llm_proposed"])
    return out


def main(argv: list[str]) -> int:
    opts = {"--model": DEFAULT_MODEL,
            "--observations": "obs-data/observations.csv",
            "--out": "proposals/merges", "--cache-dir": "llm_cache"}
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
        print("usage: python3 merge_assistant.py <run_dir> [--model ID] "
              "[--observations PATH] [--out proposals/merges] "
              "[--cache-dir llm_cache]", file=sys.stderr)
        return 2
    import langfuse_sink
    out = assist(positional[0], opts["--model"],
                 observations_path=opts["--observations"],
                 out_root=opts["--out"], cache_dir=opts["--cache-dir"],
                 sink=(langfuse_sink.emit_tool_trace
                       if langfuse_sink.enabled() else None))
    proposal = json.loads((out / "proposal.json").read_text(encoding="utf-8"))
    print(out)
    print(f"cases={len(proposal['cases'])} "
          f"verdicts={len(proposal['accepted'])}")
    if proposal.get("trace_url"):
        print(f"trace: {proposal['trace_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
