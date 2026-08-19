#!/usr/bin/env python3
"""§2 alias mining (free-thinking-llm-options.md, priority item #4).

Offline. The out-of-vocab claim values of a WRITTEN run are the mining
backlog -- claims.csv carries `in_vocab` per claim precisely because claims
are not vocabulary-constrained (the evidence that the vocabulary is
incomplete must survive to here). The LLM triages that backlog into:

- **aliases** -- surface strings that are existing vocabulary members under
  another name ("wisenet" -> Hanwha Vision). Accepted entries become a
  proposed diff to normalization.yaml, validated so every target is already
  a vocabulary member (the same check loader.py enforces at load time).
- **new_vocab** -- genuinely new vendors/device types. These are ADVISORY
  ONLY: the tool never edits canonical_vocab.csv, because §7.6 treats
  vocabulary narrowing/widening as a versioned event a human owns.

Same contract as rule_compiler: pin-and-cache per model, `origin:
llm_proposed` provenance, human merge, and the proposal dir is
`compare`-compatible so model-vs-model metrics run through the existing
eval harness unchanged (`rule_compiler.py compare` accepts these dirs).
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

import yaml

import llm_client
from llm_client import DEFAULT_MODEL, call_model, parse_response
from obs_pipeline.loader import load_rules
from obs_pipeline.normalize import _surface_key
from rule_compiler import write_candidate_rules

_ALIAS_BLOCKS = {
    "vendor_alias": "vendor",
    "oem_rebrand": "vendor",
    "device_type_alias": "device_type",
}

_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["aliases", "new_vocab"],
    "properties": {
        "aliases": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["block", "surface", "target", "rationale"],
                "properties": {
                    "block": {"type": "string",
                              "enum": sorted(_ALIAS_BLOCKS)},
                    "surface": {"type": "string"},
                    "target": {"type": "string"},
                    "rationale": {"type": "string"},
                },
            },
        },
        "new_vocab": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["column", "value", "rationale"],
                "properties": {
                    "column": {"type": "string",
                               "enum": ["vendor", "device_type"]},
                    "value": {"type": "string"},
                    "rationale": {"type": "string"},
                },
            },
        },
    },
}


def collect_backlog(run_dir) -> list[dict]:
    """Distinct out-of-vocab (key, value) pairs from claims.csv, with claim
    counts and sample obs ids. Reads the written bundle, not a re-run, for
    the same invariant-#4 reason as rule_compiler.collect_backlog."""
    seen: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"count": 0, "obs_ids": set()})
    with open(Path(run_dir) / "claims.csv", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["kind"] != "field" or row["in_vocab"] != "False":
                continue
            entry = seen[(row["key"], row["value"])]
            entry["count"] += 1
            entry["obs_ids"].add(row["obs_id"])
    return [{"key": k, "value": v, "count": e["count"],
             "obs_ids": sorted(e["obs_ids"])[:3]}
            for (k, v), e in sorted(seen.items())]


def build_prompt(backlog: list[dict], rules) -> str:
    lines = [
        "You are curating the closed value vocabularies of a device-",
        "fingerprinting pipeline. The pipeline observed the values below as",
        "claims, but they are not members of the canonical vocabulary. For",
        "each value decide whether it is (a) an alias of an EXISTING",
        "vocabulary member -- a rebrand, sub-brand, abbreviation or spelling",
        "variant -- or (b) a genuinely new vendor/device type, or (c) noise",
        "(protocol fragments, truncation) that should map to nothing.",
        "",
        "Canonical vendors:",
        *(f"- {v}" for v in sorted(rules.vocab.vendors)),
        "",
        "Canonical device types:",
        *(f"- {d}" for d in sorted(rules.vocab.device_types)),
        "",
        "Existing alias maps (do NOT re-propose these):",
    ]
    for block in sorted(_ALIAS_BLOCKS):
        amap = (rules.normalization.get(block, {}).get("map") or {})
        for surface in sorted(amap):
            lines.append(f"- {block}: {surface!r} -> {amap[surface]!r}")
    lines += [
        "",
        "Rules for `aliases` entries: `surface` must be one of the observed",
        "values below, lowercased; `target` must be an EXISTING canonical",
        "value from the lists above; `block` is vendor_alias or oem_rebrand",
        "for vendors (oem_rebrand only for true OEM/parent relationships),",
        "device_type_alias for device types. Genuinely new values go in",
        "`new_vocab` instead (they will be human-reviewed, never merged",
        "automatically). Noise values: simply omit them.",
        "",
        "Observed out-of-vocabulary values:",
    ]
    for item in backlog:
        lines.append(f"- key={item['key']} value={item['value']!r} "
                     f"claims={item['count']} sample_obs={item['obs_ids']}")
    return "\n".join(lines)


def validate_alias(alias: dict, rules, taken: set[tuple[str, str]],
                   backlog: list[dict]) -> list[str]:
    reasons = []
    block = alias.get("block")
    column = _ALIAS_BLOCKS.get(block)
    if column is None:
        reasons.append(f"unknown block {block!r}")
        return reasons
    surface = alias.get("surface", "")
    if surface != _surface_key(surface):
        reasons.append(f"surface {surface!r} is not in lookup-key form "
                       f"(lowercase, collapsed whitespace)")
    if surface in (rules.normalization.get(block, {}).get("map") or {}):
        reasons.append(f"surface {surface!r} already mapped in {block}")
    if (block, surface) in taken:
        reasons.append(f"duplicate proposal for {block}:{surface}")
    pool = (rules.vocab.vendors if column == "vendor"
            else rules.vocab.device_types)
    if alias.get("target") not in pool:
        reasons.append(f"target {alias.get('target')!r} is not an existing "
                       f"{column} in canonical_vocab.csv (new values belong "
                       f"in new_vocab)")
    observed = {_surface_key(i["value"]) for i in backlog
                if i["key"] == column}
    if _surface_key(surface) not in observed:
        reasons.append(f"surface {surface!r} was not observed in the backlog")
    return reasons


def propose(run_dir, model=DEFAULT_MODEL, *, rules_dir="rules",
            out_root="proposals/aliases", cache_dir="llm_cache",
            transport=None, sink=None) -> Path:
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text(
        encoding="utf-8"))
    rules = load_rules(rules_dir)
    backlog = collect_backlog(run_dir)

    out = Path(out_root) / model
    out.mkdir(parents=True, exist_ok=True)

    proposal = {
        "origin": "llm_proposed",
        "model": model,
        "run_id": manifest["run_id"],
        "rules_rollup": manifest["rules_rollup"],
        "backlog": backlog,
        "accepted": {}, "rejected": [], "new_vocab": [],
        "prompt_hash": None, "cache_key": None, "cached": None, "usage": None,
    }

    aliases: dict[str, dict[str, str]] = {}
    if backlog:
        prompt = build_prompt(backlog, rules)
        body = llm_client.request_body(model, prompt, _RESPONSE_SCHEMA)
        resp, key, cached, elapsed_ms = call_model(body, cache_dir, transport)
        parsed = parse_response(resp)
        proposal.update(prompt_hash=llm_client.sha256(prompt.encode("utf-8")),
                        cache_key=key, cached=cached,
                        usage=resp.get("usage"),
                        new_vocab=parsed.get("new_vocab", []))
        taken: set[tuple[str, str]] = set()
        for alias in parsed.get("aliases", []):
            reasons = validate_alias(alias, rules, taken, backlog)
            if reasons:
                proposal["rejected"].append({"rule": alias,
                                             "reasons": reasons})
                continue
            taken.add((alias["block"], alias["surface"]))
            # compare() lists proposal["accepted"] keys, so key on
            # block:surface to stay unambiguous across blocks.
            proposal["accepted"][f"{alias['block']}:{alias['surface']}"] = {
                **alias, "origin": "llm_proposed", "model": model}
            aliases.setdefault(alias["block"], {})[alias["surface"]] = \
                alias["target"]

    if sink and proposal["cache_key"]:
        proposal["trace_url"] = sink(
            tool="alias_miner", model=model, prompt=prompt,
            output_text=json.dumps(parsed, indent=2, sort_keys=True),
            usage=proposal["usage"], cached=cached,
            cache_key=proposal["cache_key"], elapsed_ms=elapsed_ms,
            metadata={"run_id": manifest["run_id"],
                      "rules_rollup": manifest["rules_rollup"]},
            spans=[("collect_backlog", {"values": len(backlog)}),
                   ("validate", {"accepted": len(proposal["accepted"]),
                                 "rejected": len(proposal["rejected"]),
                                 "new_vocab": len(proposal["new_vocab"])})],
            scores=[("accepted_aliases", len(proposal["accepted"])),
                    ("rejected_aliases", len(proposal["rejected"])),
                    ("new_vocab_nominations", len(proposal["new_vocab"]))],
            session_id=manifest["run_id"])

    (out / "proposal.json").write_text(
        json.dumps(proposal, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")

    # Human-review artifact in normalization.yaml's own shape; merging it is
    # a manual act, exactly as with rule_compiler's fragment.
    fragment = {block: {"map": entries}
                for block, entries in sorted(aliases.items())}
    (out / "proposed_normalization.yaml").write_text(
        yaml.safe_dump(fragment, sort_keys=True), encoding="utf-8")

    write_candidate_rules(out / "rules", rules_dir,
                          normalization_aliases=aliases)
    return out


def apply_aliases(proposal_dir, rules_dir) -> int:
    """Merge a proposal's ACCEPTED aliases into `rules_dir` -- the alias
    analogue of rule_compiler.apply_proposal, with the same contract:
    explicit invocation, post-merge load_rules validation, VERSION bump.
    new_vocab nominations are never applied here; vocabulary changes stay a
    human edit to canonical_vocab.csv."""
    from rule_compiler import bump_version
    proposal = json.loads((Path(proposal_dir) / "proposal.json").read_text(
        encoding="utf-8"))
    aliases: dict[str, dict[str, str]] = {}
    for entry in proposal["accepted"].values():
        aliases.setdefault(entry["block"], {})[entry["surface"]] = \
            entry["target"]
    if aliases:
        dest = Path(rules_dir) / "normalization.yaml"
        parsed = yaml.safe_load(dest.read_text(encoding="utf-8")) or {}
        for block, entries in aliases.items():
            parsed.setdefault(block, {}).setdefault("map", {}).update(entries)
        dest.write_text(yaml.safe_dump(parsed, sort_keys=True),
                        encoding="utf-8")
        bump_version(rules_dir)
        load_rules(rules_dir)   # CrossFileError here means do not proceed
    return sum(len(v) for v in aliases.values())


def main(argv: list[str]) -> int:
    opts = {"--model": DEFAULT_MODEL, "--rules-dir": "rules",
            "--out": "proposals/aliases", "--cache-dir": "llm_cache"}
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
        applied = apply_aliases(positional[1], positional[2])
        print(f"applied {applied} aliases to {positional[2]}")
        return 0
    if len(positional) != 1:
        print("usage: python3 alias_miner.py <run_dir> [--model ID] "
              "[--rules-dir rules] [--out proposals/aliases] "
              "[--cache-dir llm_cache]\n"
              "       python3 alias_miner.py apply <proposal_dir> "
              "<rules_dir>", file=sys.stderr)
        return 2
    import langfuse_sink
    out = propose(positional[0], opts["--model"], rules_dir=opts["--rules-dir"],
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
