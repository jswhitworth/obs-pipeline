#!/usr/bin/env python3
"""§4 label QA sweep (free-thinking-llm-options.md, priority item #2).

Offline auditor over the ground truth: for every label, the LLM sees the
underlying observation and flags rows where the payload plainly says
something else (payload-label contradiction) or where labels contradict
each other (internal consistency: same_device pairs with conflicting field
labels, values with no supporting evidence in the payload).

**Propose, don't overwrite** -- the doc's own hard recommendation, and the
labels' independence is what makes eval numbers mean anything. The output
is label_suspects.csv (row, suspected error, proposed fix, evidence quote,
LLM confidence); acceptance flows through the EXISTING adjudication path
(adjudicate.py), never through this tool. Nothing here writes to labels/.

This tool reads labels and observations but no pipeline output at all, so
it cannot launder pipeline opinion into ground truth (the invariant-#6
concern): a suspect is grounded in the payload text or in another label,
never in what the pipeline resolved.

Model comparison: run once per --model; each writes its own proposal dir,
and suspects carry the model id, so two sweeps diff directly.
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

import llm_client
from llm_client import DEFAULT_MODEL, call_model, parse_response
from label_tools import labels_hash, load_labels

_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["suspects"],
    "properties": {
        "suspects": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["obs_id", "key", "current_value",
                             "proposed_value", "suspected_error",
                             "evidence", "confidence"],
                "properties": {
                    "obs_id": {"type": "string"},
                    "key": {"type": "string"},
                    "current_value": {"type": "string"},
                    "proposed_value": {"type": "string"},
                    "suspected_error": {"type": "string"},
                    "evidence": {"type": "string"},
                    "confidence": {"type": "string",
                                   "enum": ["low", "medium", "high"]},
                },
            },
        },
    },
}

_CSV_COLUMNS = ("obs_id", "key", "current_value", "proposed_value",
                "suspected_error", "evidence", "llm_confidence", "model",
                "origin")


def build_prompt(labels: list[dict], observations: list[dict]) -> str:
    by_obs: dict[str, list[dict]] = defaultdict(list)
    pairs = []
    for lab in labels:
        if lab.get("key") == "same_device":
            pairs.append(tuple(sorted([lab["obs_id"], lab["value"]])))
        else:
            by_obs[lab["obs_id"]].append(lab)
    obs_by_id = {o["obs_id"]: o for o in observations}

    lines = [
        "You are auditing hand-made ground-truth labels for a device-",
        "fingerprinting dataset. For each observation you get the raw",
        "evidence (source, payload, mac, hostname, open ports) and the",
        "labels a human assigned. Flag ONLY rows you suspect are labeling",
        "errors: the payload plainly says something else, the label",
        "contradicts a same-device partner's label, a value appears",
        "copy-pasted from a neighbouring row, or a firmware/model is",
        "labeled that no evidence supports. Typical error modes: right",
        "column wrong row, copy-paste drift, inconsistent vendor spelling,",
        "stale values.",
        "",
        "For each suspect: quote the exact evidence substring, propose the",
        "corrected value, and rate your confidence. `current_value` must",
        "repeat the label's value EXACTLY as given below. Do not flag rows",
        "merely because evidence is thin -- a low-certainty label with no",
        "contradicting evidence is not an error. An empty suspects list is",
        "a valid answer.",
        "",
        "same_device pairs (human judgement that two observations are the",
        "same physical device):",
        *(f"- {a} <-> {b}" for a, b in sorted(set(pairs))),
        "",
        "Observations and their labels:",
    ]
    for obs_id in sorted(by_obs):
        o = obs_by_id.get(obs_id, {})
        lines.append(
            f"- {obs_id} source={o.get('source', '?')} "
            f"payload={o.get('raw_payload', '')!r} mac={o.get('mac', '')} "
            f"hostname={o.get('hostname', '')} ports={o.get('open_ports', '')}")
        for lab in sorted(by_obs[obs_id], key=lambda l: l["key"]):
            lines.append(f"    label {lab['key']} = {lab['value']!r} "
                         f"(certainty={lab['labeler_certainty']})")
    return "\n".join(lines)


def validate_suspect(s: dict, labels: list[dict]) -> list[str]:
    """A suspect must point at a real label row and quote its value
    correctly -- a mis-quoted current_value means the model audited a row
    that does not exist, and a human acting on it would mis-target."""
    reasons = []
    if s.get("key") == "same_device":
        pairs = {tuple(sorted([l["obs_id"], l["value"]])) for l in labels
                 if l.get("key") == "same_device"}
        if tuple(sorted([s.get("obs_id", ""),
                         s.get("current_value", "")])) not in pairs:
            reasons.append("no such same_device pair in labels.csv")
        return reasons
    match = [l for l in labels
             if l["obs_id"] == s.get("obs_id") and l["key"] == s.get("key")]
    if not match:
        reasons.append(f"no label row for ({s.get('obs_id')}, {s.get('key')})")
    elif match[0]["value"] != s.get("current_value"):
        reasons.append(f"current_value {s.get('current_value')!r} does not "
                       f"match the label's actual value {match[0]['value']!r}")
    if s.get("proposed_value") == s.get("current_value"):
        reasons.append("proposed_value equals current_value")
    return reasons


def sweep(labels_path="labels/labels.csv",
          observations_path="obs-data/observations.csv",
          model=DEFAULT_MODEL, *, out_root="proposals/labels",
          cache_dir="llm_cache", transport=None, sink=None) -> Path:
    labels = load_labels(labels_path)
    with open(observations_path, newline="", encoding="utf-8") as fh:
        observations = list(csv.DictReader(fh))

    out = Path(out_root) / model
    out.mkdir(parents=True, exist_ok=True)

    prompt = build_prompt(labels, observations)
    body = llm_client.request_body(model, prompt, _RESPONSE_SCHEMA)
    resp, key, cached, elapsed_ms = call_model(body, cache_dir, transport)
    parsed = parse_response(resp)

    accepted, rejected = [], []
    for s in parsed.get("suspects", []):
        reasons = validate_suspect(s, labels)
        (rejected if reasons else accepted).append(
            {"suspect": s, "reasons": reasons} if reasons else s)

    proposal = {
        "origin": "llm_proposed",
        "model": model,
        # The sweep is against a specific frozen label set; the hash makes a
        # stale suspects file self-evident after labels move (§7.6's frozen-
        # label-set assumption, applied to this artifact).
        "labels_hash": labels_hash(labels_path),
        "prompt_hash": llm_client.sha256(prompt.encode("utf-8")),
        "cache_key": key, "cached": cached, "usage": resp.get("usage"),
        "accepted_count": len(accepted), "rejected": rejected,
    }
    if sink:
        proposal["trace_url"] = sink(
            tool="label_qa", model=model, prompt=prompt,
            output_text=json.dumps(parsed, indent=2, sort_keys=True),
            usage=resp.get("usage"), cached=cached, cache_key=key,
            elapsed_ms=elapsed_ms,
            metadata={"labels_hash": proposal["labels_hash"]},
            spans=[("validate", {"accepted": len(accepted),
                                 "rejected": len(rejected)})],
            scores=[("suspects", len(accepted)),
                    ("rejected_suspects", len(rejected))],
            session_id=proposal["labels_hash"])
    (out / "proposal.json").write_text(
        json.dumps(proposal, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")

    with open(out / "label_suspects.csv", "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(_CSV_COLUMNS)
        for s in sorted(accepted, key=lambda s: (s["obs_id"], s["key"])):
            w.writerow([s["obs_id"], s["key"], s["current_value"],
                        s["proposed_value"], s["suspected_error"],
                        s["evidence"], s["confidence"], model,
                        "llm_proposed"])
    return out


def main(argv: list[str]) -> int:
    opts = {"--model": DEFAULT_MODEL, "--labels": "labels/labels.csv",
            "--observations": "obs-data/observations.csv",
            "--out": "proposals/labels", "--cache-dir": "llm_cache"}
    i = 0
    while i < len(argv):
        if argv[i] in opts:
            opts[argv[i]] = argv[i + 1]
            i += 2
        else:
            print("usage: python3 label_qa.py [--model ID] [--labels PATH] "
                  "[--observations PATH] [--out proposals/labels] "
                  "[--cache-dir llm_cache]", file=sys.stderr)
            return 2
    import langfuse_sink
    out = sweep(opts["--labels"], opts["--observations"], opts["--model"],
                out_root=opts["--out"], cache_dir=opts["--cache-dir"],
                sink=(langfuse_sink.emit_tool_trace
                      if langfuse_sink.enabled() else None))
    print(out)
    proposal = json.loads((out / "proposal.json").read_text(encoding="utf-8"))
    if proposal.get("trace_url"):
        print(f"trace: {proposal['trace_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
