#!/usr/bin/env python3
"""§6 rule authoring copilot (free-thinking-llm-options.md, priority item #3).

Offline. The eval harness already closes the loop mechanically (run ->
score -> regressions surfaced); this tool sits in the expensive human step
in the middle: reading a miss and writing the rule diff.

- Input: an eval dir's incorrect outcomes + the observations behind them +
  the current extraction.yaml / normalization.yaml text.
- Output: a candidate rule diff (new extraction rules and/or alias map
  entries) with a plain-English rationale per patch.
- Validation: the EXISTING eval harness as fitness function -- the
  candidate is scored against the input eval's own outcomes as baseline,
  and the diff is SURFACED only when it fixes at least one target without
  breaking the stable-correct bucket (four_bucket.broken == 0). A gated-out
  diff is still written, marked surfaced=false, so the miss is auditable.

eval.py's LabelsMovedError guard applies unchanged: if labels moved since
the input eval, the compare refuses rather than misattributing the delta.
Human merge remains the acceptance step; this tool never writes to rules/.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import yaml

import llm_client
from llm_client import DEFAULT_MODEL, call_model, parse_response
from obs_pipeline.loader import load_rules
from alias_miner import _ALIAS_BLOCKS, validate_alias
from rule_compiler import validate_rule, write_candidate_rules

_RULE_ITEM = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "source", "pattern", "target_kind", "target",
                 "rationale", "tests"],
    "properties": {
        "name": {"type": "string"},
        "source": {"type": "string"},
        "pattern": {"type": "string"},
        "target_kind": {"type": "string", "enum": ["field", "link_basis"]},
        "target": {"type": "string"},
        "rationale": {"type": "string"},
        "tests": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["payload", "expect"],
            "properties": {"payload": {"type": "string"},
                           "expect": {"type": "string"}}}},
    },
}

_ALIAS_ITEM = {
    "type": "object",
    "additionalProperties": False,
    "required": ["block", "surface", "target", "rationale"],
    "properties": {
        "block": {"type": "string", "enum": sorted(_ALIAS_BLOCKS)},
        "surface": {"type": "string"},
        "target": {"type": "string"},
        "rationale": {"type": "string"},
    },
}

_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["extraction_rules", "aliases", "notes"],
    "properties": {
        "extraction_rules": {"type": "array", "items": _RULE_ITEM},
        "aliases": {"type": "array", "items": _ALIAS_ITEM},
        "notes": {"type": "string"},
    },
}


def collect_targets(eval_dir) -> list[dict]:
    """The incorrect field outcomes of a written eval -- the misses the
    copilot is asked to fix. Undecidable rows (correct=None) are excluded
    for the same reason eval.py excludes them from denominators."""
    outcomes = json.loads((Path(eval_dir) / "outcomes.json").read_text(
        encoding="utf-8"))
    return sorted((o for o in outcomes if o["correct"] is False),
                  key=lambda o: (o["obs_id"], o["key"]))


def build_prompt(targets: list[dict], observations: list[dict],
                 rules_dir) -> str:
    obs_by_id = {o["obs_id"]: o for o in observations}
    rules_dir = Path(rules_dir)
    lines = [
        "You are a rule-authoring copilot for a deterministic device-",
        "fingerprinting pipeline. Below are labeled cases the pipeline",
        "currently resolves INCORRECTLY, each with the raw observation",
        "behind it, plus the full current extraction and normalization rule",
        "files. Propose the smallest rule diff that would fix some of these",
        "cases: new extraction rules (Python `re` regex over raw_payload,",
        "named group `v` capturing exactly the value substring) and/or new",
        "alias map entries (surface -> canonical, lowercase surface form).",
        "Do not modify or remove existing rules; only additions are",
        "possible. Do not propose a patch for a case whose payload simply",
        "lacks the evidence -- leave those alone and say so in `notes`.",
        "Every proposal will be scored by an eval harness and discarded if",
        "it breaks any currently-correct case, so prefer precise patterns",
        "and exact alias surfaces over broad ones.",
        "",
        "Misresolved cases:",
    ]
    for t in targets:
        o = obs_by_id.get(t["obs_id"], {})
        lines.append(
            f"- {t['obs_id']} field={t['key']} expected={t['expected']!r} "
            f"actual={t['actual']!r} provenance={t['provenance']} "
            f"top_claim={t.get('top1_claim', '')!r}")
        lines.append(
            f"    source={o.get('source', '?')} "
            f"payload={o.get('raw_payload', '')!r} mac={o.get('mac', '')} "
            f"hostname={o.get('hostname', '')} ports={o.get('open_ports', '')}")
    lines += [
        "",
        "--- current extraction.yaml ---",
        (rules_dir / "extraction.yaml").read_text(encoding="utf-8"),
        "--- current normalization.yaml ---",
        (rules_dir / "normalization.yaml").read_text(encoding="utf-8"),
    ]
    return "\n".join(lines)


def _alias_backlog(targets: list[dict]) -> list[dict]:
    """Adapt targets into alias_miner.validate_alias's backlog shape: the
    observed surface evidence an alias may cite is the misresolved case's
    own actual value or top-ranked claim -- never a string from nowhere."""
    out = []
    for t in targets:
        for value in (t.get("actual"), t.get("top1_claim")):
            if value:
                out.append({"key": t["key"], "value": value})
    return out


def propose(eval_dir, model=DEFAULT_MODEL, *, observations_path,
            rules_dir="rules", labels_path="labels/labels.csv",
            out_root="proposals/copilot", cache_dir="llm_cache",
            transport=None, sink=None) -> Path:
    from eval import evaluate  # deferred, mirroring rule_compiler.compare

    eval_dir = Path(eval_dir)
    eval_manifest = json.loads((eval_dir / "eval_manifest.json").read_text(
        encoding="utf-8"))
    rules = load_rules(rules_dir)
    targets = collect_targets(eval_dir)
    with open(observations_path, newline="", encoding="utf-8") as fh:
        observations = list(csv.DictReader(fh))

    out = Path(out_root) / model
    out.mkdir(parents=True, exist_ok=True)

    proposal = {
        "origin": "llm_proposed",
        "model": model,
        "eval_dir": str(eval_dir),
        "baseline_run_id": eval_manifest["run_id"],
        "labels_hash": eval_manifest["labels_hash"],
        "targets": [{"obs_id": t["obs_id"], "key": t["key"],
                     "expected": t["expected"], "actual": t["actual"]}
                    for t in targets],
        "accepted": {}, "rejected": [], "notes": "",
        "prompt_hash": None, "cache_key": None, "cached": None, "usage": None,
        "surfaced": False, "four_bucket": None,
    }

    extraction_rules: dict = {}
    aliases: dict[str, dict[str, str]] = {}
    if targets:
        prompt = build_prompt(targets, observations, rules_dir)
        # Dozens of targets plus two full rule files: thinking + patches
        # need more room than the shared default before truncating (Sonnet 5
        # tokenizes ~30% heavier than Opus and truncated at 32K here).
        body = llm_client.request_body(model, prompt, _RESPONSE_SCHEMA,
                                       max_tokens=64000)
        resp, key, cached, elapsed_ms = call_model(body, cache_dir, transport)
        parsed = parse_response(resp)
        proposal.update(prompt_hash=llm_client.sha256(prompt.encode("utf-8")),
                        cache_key=key, cached=cached,
                        usage=resp.get("usage"),
                        notes=parsed.get("notes", ""))

        # Rule patches must match a target's payload; the targets' obs rows
        # stand in for rule_compiler's backlog.
        target_obs = {t["obs_id"] for t in targets}
        rule_backlog = [o for o in observations if o["obs_id"] in target_obs]
        taken: set[str] = set()
        for rule in parsed.get("extraction_rules", []):
            reasons = validate_rule(rule, rules, taken, rule_backlog)
            if reasons:
                proposal["rejected"].append({"rule": rule, "reasons": reasons})
                continue
            taken.add(rule["name"])
            extraction_rules[rule["name"]] = {
                "sources": [rule["source"]], "pattern": rule["pattern"],
                "target_kind": rule["target_kind"], "target": rule["target"],
                "origin": "llm_proposed", "model": model,
                "rationale": rule["rationale"], "tests": rule["tests"],
            }
            proposal["accepted"][f"rule:{rule['name']}"] = \
                extraction_rules[rule["name"]]

        alias_backlog = _alias_backlog(targets)
        taken_aliases: set[tuple[str, str]] = set()
        for alias in parsed.get("aliases", []):
            reasons = validate_alias(alias, rules, taken_aliases,
                                     alias_backlog)
            if reasons:
                proposal["rejected"].append({"rule": alias,
                                             "reasons": reasons})
                continue
            taken_aliases.add((alias["block"], alias["surface"]))
            aliases.setdefault(alias["block"], {})[alias["surface"]] = \
                alias["target"]
            proposal["accepted"][f"alias:{alias['block']}:{alias['surface']}"] \
                = {**alias, "origin": "llm_proposed", "model": model}

    write_candidate_rules(out / "rules", rules_dir,
                          extraction_rules=extraction_rules,
                          normalization_aliases=aliases)

    if proposal["accepted"]:
        # The mechanical gate: score the candidate against the input eval's
        # own outcomes. LabelsMovedError propagates -- a moved label set
        # must refuse, not misattribute (§7.6).
        base_outcomes = json.loads((eval_dir / "outcomes.json").read_text(
            encoding="utf-8"))
        gate_eval = evaluate(
            observations_path, out / "rules", labels_path, out / "gate",
            runs_root=out / "gate" / "runs",
            baseline_outcomes=base_outcomes,
            baseline_labels_hash=eval_manifest["labels_hash"],
            baseline_run_id=eval_manifest["run_id"],
            changed_rule_files=("extraction.yaml", "normalization.yaml"))
        four_bucket = json.loads((gate_eval / "four_bucket.json").read_text(
            encoding="utf-8"))
        proposal["four_bucket"] = four_bucket
        proposal["gate_eval_dir"] = str(gate_eval)
        proposal["surfaced"] = (four_bucket["fixed"] > 0
                                and four_bucket["broken"] == 0)

    if sink and proposal["cache_key"]:
        fb = proposal["four_bucket"] or {}
        proposal["trace_url"] = sink(
            tool="rule_copilot", model=model, prompt=prompt,
            output_text=json.dumps(parsed, indent=2, sort_keys=True),
            usage=proposal["usage"], cached=cached,
            cache_key=proposal["cache_key"], elapsed_ms=elapsed_ms,
            metadata={"eval_dir": str(eval_dir),
                      "baseline_run_id": eval_manifest["run_id"],
                      "labels_hash": eval_manifest["labels_hash"]},
            spans=[("collect_targets", {"targets": len(targets)}),
                   ("validate", {"accepted": len(proposal["accepted"]),
                                 "rejected": len(proposal["rejected"])}),
                   ("gate_eval", {"four_bucket": proposal["four_bucket"],
                                  "surfaced": proposal["surfaced"]})],
            scores=[("fixed", fb.get("fixed", 0)),
                    ("broken", fb.get("broken", 0)),
                    ("surfaced", int(proposal["surfaced"])),
                    ("accepted_patches", len(proposal["accepted"]))],
            session_id=eval_manifest["run_id"])

    (out / "proposal.json").write_text(
        json.dumps(proposal, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    (out / "proposed_extraction_rules.yaml").write_text(
        yaml.safe_dump({"rules": extraction_rules}, sort_keys=True),
        encoding="utf-8")
    (out / "proposed_normalization.yaml").write_text(
        yaml.safe_dump({b: {"map": m} for b, m in sorted(aliases.items())},
                       sort_keys=True), encoding="utf-8")
    return out


def apply_patches(proposal_dir, rules_dir) -> int:
    """Merge a SURFACED copilot diff into `rules_dir`. The accepted dict
    mixes extraction rules (`rule:` keys) and aliases (`alias:` keys), so
    neither rule_compiler.apply_proposal nor alias_miner.apply_aliases can
    apply it alone; this splits and merges both sides in one apply (one
    VERSION bump, one post-merge validation). Refuses a diff the gate did
    not surface -- applying an ungated diff would bypass the fitness
    function that justifies the tool."""
    from rule_compiler import bump_version
    proposal = json.loads((Path(proposal_dir) / "proposal.json").read_text(
        encoding="utf-8"))
    if not proposal.get("surfaced"):
        raise RuntimeError(
            "proposal was not surfaced by the eval gate (fixed>0 and "
            "broken==0); refusing to apply an ungated diff")
    rules_patch, alias_patch = {}, {}
    for key, entry in proposal["accepted"].items():
        if key.startswith("rule:"):
            rules_patch[key[len("rule:"):]] = entry
        elif key.startswith("alias:"):
            alias_patch.setdefault(entry["block"], {})[entry["surface"]] = \
                entry["target"]
    dest = Path(rules_dir)
    if rules_patch:
        parsed = yaml.safe_load((dest / "extraction.yaml").read_text(
            encoding="utf-8")) or {}
        parsed.setdefault("rules", {}).update(rules_patch)
        (dest / "extraction.yaml").write_text(
            yaml.safe_dump(parsed, sort_keys=True), encoding="utf-8")
    if alias_patch:
        parsed = yaml.safe_load((dest / "normalization.yaml").read_text(
            encoding="utf-8")) or {}
        for block, entries in alias_patch.items():
            parsed.setdefault(block, {}).setdefault("map", {}).update(entries)
        (dest / "normalization.yaml").write_text(
            yaml.safe_dump(parsed, sort_keys=True), encoding="utf-8")
    if rules_patch or alias_patch:
        bump_version(rules_dir)
        load_rules(rules_dir)   # CrossFileError here means do not proceed
    return len(rules_patch) + sum(len(v) for v in alias_patch.values())


def main(argv: list[str]) -> int:
    opts = {"--model": DEFAULT_MODEL, "--rules-dir": "rules",
            "--observations": "obs-data/observations.csv",
            "--labels": "labels/labels.csv",
            "--out": "proposals/copilot", "--cache-dir": "llm_cache"}
    positional = []
    i = 0
    while i < len(argv):
        if argv[i] in opts:
            opts[argv[i]] = argv[i + 1]
            i += 2
        else:
            positional.append(argv[i])
            i += 1
    if len(positional) == 3 and positional[0] == "apply":
        applied = apply_patches(positional[1], positional[2])
        print(f"applied {applied} patches to {positional[2]}")
        return 0
    if len(positional) != 1:
        print("usage: python3 rule_copilot.py <eval_dir> [--model ID] "
              "[--observations PATH] [--rules-dir rules] [--labels PATH] "
              "[--out proposals/copilot] [--cache-dir llm_cache]\n"
              "       python3 rule_copilot.py apply <proposal_dir> "
              "<rules_dir>", file=sys.stderr)
        return 2
    import langfuse_sink
    out = propose(positional[0], opts["--model"],
                  observations_path=opts["--observations"],
                  rules_dir=opts["--rules-dir"], labels_path=opts["--labels"],
                  out_root=opts["--out"], cache_dir=opts["--cache-dir"],
                  sink=(langfuse_sink.emit_tool_trace
                        if langfuse_sink.enabled() else None))
    proposal = json.loads((out / "proposal.json").read_text(encoding="utf-8"))
    print(out)
    print(f"surfaced={proposal['surfaced']} four_bucket="
          f"{proposal['four_bucket']}")
    if proposal.get("trace_url"):
        print(f"trace: {proposal['trace_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
