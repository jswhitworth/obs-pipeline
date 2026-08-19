#!/usr/bin/env python3
"""The §1c rule compiler (free-thinking-llm-options.md, item #1).

Offline only. An LLM reads the `no_extraction` backlog from a WRITTEN run
bundle and proposes extraction rules (patterns + tests) as reviewable
artifacts. The LLM never runs at pipeline time: run.py and everything it
imports have no path into this module, so invariant #5's shape is preserved
-- the runtime stays deterministic and the model's knowledge is distilled
into auditable YAML a human merges by hand.

Three properties this file is responsible for, from the options doc §7:

- **Pin and cache.** Every API call is keyed on the sha256 of the exact
  request body (model id + prompt + schema), and the response is cached.
  Reruns over identical input never re-sample the model; switching models is
  an explicit, diffable event (a new cache key and a new proposal dir), not
  silent drift.
- **Provenance.** Every proposed rule carries `origin: llm_proposed` and the
  model id, both in proposal.json and on the YAML fragment itself, so "how
  much of what we believe came from the model" stays answerable later.
- **Mechanical gating.** Proposals are validated locally (regex compiles,
  named group `v`, declared source/target, tests pass, matches a backlog
  payload) and then scored by the EXISTING eval harness -- `compare` runs
  eval.evaluate per model against a shared baseline, so the four-bucket diff
  decides whether a model's rules fixed the backlog without breaking the
  stable-correct bucket. This module adds no second scoring rule.

Model comparison: `propose` is run once per model (--model); `compare` takes
the resulting proposal dirs and emits one comparison.json / comparison.md
over the same observations, rules baseline and labels.

stdlib + PyYAML only (repo constraint), so the Anthropic Messages API is
called over urllib rather than the SDK.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import yaml

import llm_client
from llm_client import DEFAULT_MODEL, cache_key, call_model, parse_response
from obs_pipeline.loader import RULE_FILES, VOCAB_FILE, load_rules

# Structured-output schema for the model's reply. Targets are validated
# locally against claims.yaml rather than baked into the schema, so the
# schema itself never drifts from the rule files.
_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["rules", "skipped"],
    "properties": {
        "rules": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "source", "pattern", "target_kind",
                             "target", "rationale", "tests"],
                "properties": {
                    "name": {"type": "string"},
                    "source": {"type": "string"},
                    "pattern": {"type": "string"},
                    "target_kind": {"type": "string",
                                    "enum": ["field", "link_basis"]},
                    "target": {"type": "string"},
                    "rationale": {"type": "string"},
                    "tests": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["payload", "expect"],
                            "properties": {
                                "payload": {"type": "string"},
                                "expect": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
        "skipped": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["obs_id", "reason"],
                "properties": {
                    "obs_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}


_sha256 = llm_client.sha256


# --------------------------------------------------------------------------
# Backlog collection
# --------------------------------------------------------------------------

def collect_backlog(run_dir, observations_path,
                    mode="no_extraction") -> list[dict]:
    """The unmatched-payload backlog of a written bundle, joined back to
    observation rows. Reads trace.jsonl rather than re-running extraction:
    the backlog must be the one the recorded run actually saw (invariant #4
    -- the bundle binds to a rule state; recomputing under today's rules
    could target a different backlog than the run_id claims).

    Two modes, both label-free:
    - "no_extraction": observations that produced NOTHING (the §1c default).
    - "payload_gaps": observations whose raw_payload produced no regex-rule
      extraction, even if structured lifts / OUI / ports fired. Payload-
      derived extract steps are exactly those whose evidence pointer is a
      `#raw_payload[start:end]` span (§9.4), which is what makes this
      distinguishable from the trace alone. This is the rebuild-demo mode:
      with the regex rules ablated, the mac-column lift still succeeds, so
      `no_extraction` alone would hide almost the whole gap.
    """
    import csv
    with open(observations_path, newline="", encoding="utf-8") as fh:
        rows = {r["obs_id"]: r for r in csv.DictReader(fh)}

    flagged, payload_matched = set(), set()
    with open(Path(run_dir) / "trace.jsonl", encoding="utf-8") as fh:
        for line in fh:
            step = json.loads(line)
            op = step.get("op")
            if op not in ("no_extraction", "extract"):
                continue
            for ref in step.get("inputs", []):
                if not ref.startswith("obs:"):
                    continue
                obs_id = ref[len("obs:"):].split("#")[0]
                if op == "no_extraction":
                    flagged.add(obs_id)
                elif "#raw_payload[" in ref:
                    payload_matched.add(obs_id)

    if mode == "no_extraction":
        wanted = flagged
    elif mode == "payload_gaps":
        wanted = {oid for oid, r in rows.items()
                  if (r.get("raw_payload") or "").strip()
                  and oid not in payload_matched}
    else:
        raise ValueError(f"unknown backlog mode {mode!r}")
    return sorted((rows[oid] for oid in wanted if oid in rows),
                  key=lambda r: r["obs_id"])


# --------------------------------------------------------------------------
# Prompt + request (deterministic; these bytes are the cache key)
# --------------------------------------------------------------------------

def build_prompt(cases: list[dict], rules) -> str:
    fields = sorted(rules.claims["fields"])
    bases = sorted(rules.claims["link_bases"])
    sources = sorted({c["source"] for c in cases})
    existing = []
    for name in sorted(rules.extraction.get("rules", {})):
        rule = rules.extraction["rules"][name]
        if any(s in rule["sources"] for s in sources):
            existing.append(f"- {name}: sources={sorted(rule['sources'])} "
                            f"pattern={rule['pattern']!r} -> "
                            f"{rule['target_kind']}:{rule['target']}")
    lines = [
        "You are writing extraction rules for a deterministic device-",
        "fingerprinting pipeline. Each rule is a Python `re` regex applied",
        "to an observation's raw_payload, with a named group `v` that must",
        "capture exactly the value substring (surrounding whitespace is",
        "stripped by the engine).",
        "",
        f"Declared field targets (target_kind=field): {', '.join(fields)}",
        f"Declared link_basis targets (target_kind=link_basis): {', '.join(bases)}",
        "",
        "Existing rules for the relevant sources -- do NOT duplicate these:",
        *(existing or ["(none)"]),
        "",
        "The payloads below produced NO extraction under the current rules.",
        "For each, either propose one or more new rules that extract real",
        "values present in the payload text, or list the observation under",
        "`skipped` with a reason if the payload is too truncated or",
        "ambiguous to support a reliable rule. Never invent a value that is",
        "not literally present in the payload: rules extract spans, they do",
        "not inject knowledge.",
        "",
        "Each rule needs: a snake_case name unique against the existing",
        "rules, the source it applies to, the regex pattern, target_kind,",
        "target, a one-line rationale, and at least one test whose payload",
        "the pattern matches with group `v` equal to `expect` after",
        "stripping. Prefer patterns anchored on stable delimiters over",
        "patterns tuned to one payload.",
        "",
        "Unmatched payloads:",
    ]
    for c in cases:
        lines.append(f"- obs_id={c['obs_id']} source={c['source']} "
                     f"raw_payload={c.get('raw_payload', '')!r}")
    return "\n".join(lines)


def request_body(model: str, prompt: str) -> dict:
    # 64K rather than the shared 16K default: a rebuild-size backlog can
    # legitimately need dozens of rules in one response, and Sonnet 5's
    # tokenizer truncated a 68-payload rebuild at 32K.
    return llm_client.request_body(model, prompt, _RESPONSE_SCHEMA,
                                   max_tokens=64000)


# --------------------------------------------------------------------------
# Validation -- the local half of the mechanical gate
# --------------------------------------------------------------------------

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def validate_rule(rule: dict, rules, taken: set[str],
                  backlog: list[dict]) -> list[str]:
    """Reasons the rule is unacceptable; empty means accepted. Mirrors the
    checks loader.py would raise as CrossFileError three steps later, plus
    the checks only this tool can make (tests pass, matches the backlog)."""
    reasons = []
    name = rule.get("name", "")
    if not _NAME_RE.match(name):
        reasons.append(f"name {name!r} is not snake_case")
    if name in rules.extraction.get("rules", {}) or name in taken:
        reasons.append(f"name {name!r} collides with an existing rule")
    if rule.get("source") not in rules.claims["sources"]:
        reasons.append(f"source {rule.get('source')!r} not declared in "
                       f"claims.yaml")
    pool = (rules.claims["fields"] if rule.get("target_kind") == "field"
            else rules.claims["link_bases"])
    if rule.get("target") not in pool:
        reasons.append(f"target {rule.get('target')!r} not declared for "
                       f"target_kind {rule.get('target_kind')!r}")
    try:
        compiled = re.compile(rule.get("pattern", ""))
    except re.error as e:
        return reasons + [f"pattern does not compile: {e}"]
    if "v" not in compiled.groupindex:
        reasons.append("pattern has no named group `v`")
        return reasons
    if not rule.get("tests"):
        reasons.append("no tests supplied")
    for t in rule.get("tests", []):
        m = compiled.search(t["payload"])
        if not m:
            reasons.append(f"test payload {t['payload']!r} does not match")
        elif m.group("v").strip() != t["expect"].strip():
            reasons.append(f"test extracts {m.group('v').strip()!r}, "
                           f"expected {t['expect'].strip()!r}")
    # A rule that matches none of the payloads it was asked to cover is not
    # evidence-driven -- it came from somewhere other than the backlog.
    in_scope = [c for c in backlog if c["source"] == rule.get("source")]
    if in_scope and not any(compiled.search(c.get("raw_payload") or "")
                            for c in in_scope):
        reasons.append("pattern matches no backlog payload for its source")
    return reasons


# --------------------------------------------------------------------------
# propose -- one model, one proposal dir
# --------------------------------------------------------------------------

def propose(run_dir, model=DEFAULT_MODEL, *, observations_path,
            rules_dir="rules", out_root="proposals", cache_dir="llm_cache",
            backlog_mode="no_extraction", transport=None, sink=None) -> Path:
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text(
        encoding="utf-8"))
    rules = load_rules(rules_dir)
    backlog = collect_backlog(run_dir, observations_path, backlog_mode)

    out = Path(out_root) / model
    out.mkdir(parents=True, exist_ok=True)

    proposal = {
        "origin": "llm_proposed",
        "model": model,
        "run_id": manifest["run_id"],
        "rules_rollup": manifest["rules_rollup"],
        "backlog_mode": backlog_mode,
        "backlog": [{"obs_id": c["obs_id"], "source": c["source"],
                     "raw_payload": c.get("raw_payload", "")} for c in backlog],
        "accepted": {}, "rejected": [], "skipped": [],
        "prompt_hash": None, "cache_key": None, "cached": None, "usage": None,
    }

    if backlog:
        prompt = build_prompt(backlog, rules)
        body = request_body(model, prompt)
        resp, key, cached, elapsed_ms = call_model(body, cache_dir, transport)
        parsed = parse_response(resp)
        proposal.update(prompt_hash=_sha256(prompt.encode("utf-8")),
                        cache_key=key, cached=cached,
                        usage=resp.get("usage"),
                        skipped=parsed.get("skipped", []))
        taken: set[str] = set()
        for rule in parsed.get("rules", []):
            reasons = validate_rule(rule, rules, taken, backlog)
            if reasons:
                proposal["rejected"].append({"rule": rule, "reasons": reasons})
                continue
            taken.add(rule["name"])
            proposal["accepted"][rule["name"]] = {
                "sources": [rule["source"]],
                "pattern": rule["pattern"],
                "target_kind": rule["target_kind"],
                "target": rule["target"],
                "origin": "llm_proposed",
                "model": model,
                "rationale": rule["rationale"],
                "tests": rule["tests"],
            }

    if sink and proposal["cache_key"]:
        proposal["trace_url"] = sink(
            tool="rule_compiler.propose", model=model, prompt=prompt,
            output_text=json.dumps(parsed, indent=2, sort_keys=True),
            usage=proposal["usage"], cached=cached,
            cache_key=proposal["cache_key"], elapsed_ms=elapsed_ms,
            metadata={"run_id": manifest["run_id"],
                      "rules_rollup": manifest["rules_rollup"]},
            spans=[("collect_backlog", {"cases": len(backlog)}),
                   ("validate", {"accepted": len(proposal["accepted"]),
                                 "rejected": len(proposal["rejected"]),
                                 "skipped": len(proposal["skipped"])})],
            scores=[("accepted_rules", len(proposal["accepted"])),
                    ("rejected_rules", len(proposal["rejected"]))],
            session_id=manifest["run_id"])

    (out / "proposal.json").write_text(
        json.dumps(proposal, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")

    # The human-review artifact: a fragment in extraction.yaml's own shape.
    # Merging it into rules/ is a manual act -- this tool never writes to
    # the live rules directory.
    (out / "proposed_rules.yaml").write_text(
        yaml.safe_dump({"rules": proposal["accepted"]}, sort_keys=True),
        encoding="utf-8")

    write_candidate_rules(out / "rules", rules_dir,
                          extraction_rules=proposal["accepted"])
    return out


def write_candidate_rules(dest: Path, rules_dir, *, extraction_rules=None,
                          normalization_aliases=None) -> None:
    """A throwaway rules dir for the eval harness only: the live rules plus
    proposed extraction rules and/or normalization alias entries merged in.
    Shared by the other offline tools (alias_miner, rule_copilot) so every
    proposal is scored under the same merge semantics. The extra provenance
    keys (`origin`, `model`, ...) ride along harmlessly -- extract.py reads
    only sources/pattern/target_kind/target."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    src = Path(rules_dir)
    for name in (*RULE_FILES, VOCAB_FILE, "VERSION"):
        shutil.copy(src / name, dest / name)
    if extraction_rules:
        parsed = yaml.safe_load((dest / "extraction.yaml").read_text(
            encoding="utf-8")) or {}
        parsed.setdefault("rules", {}).update(extraction_rules)
        (dest / "extraction.yaml").write_text(
            yaml.safe_dump(parsed, sort_keys=True), encoding="utf-8")
    if normalization_aliases:
        parsed = yaml.safe_load((dest / "normalization.yaml").read_text(
            encoding="utf-8")) or {}
        for block, entries in normalization_aliases.items():
            parsed.setdefault(block, {}).setdefault("map", {}).update(entries)
        (dest / "normalization.yaml").write_text(
            yaml.safe_dump(parsed, sort_keys=True), encoding="utf-8")


# --------------------------------------------------------------------------
# apply -- the sanctioned merge of an accepted fragment into a rules dir
# --------------------------------------------------------------------------

def bump_version(rules_dir) -> str:
    """Minor bump: applying accepted proposals changes outcomes, which is
    exactly eval.propose_bump's `minor` case; §6.2's hash-polices-version
    check would otherwise flag the rollup moving under an unchanged
    VERSION."""
    path = Path(rules_dir) / "VERSION"
    major, minor, _patch = (path.read_text(encoding="utf-8").strip()
                            .split("+")[0].split("-")[0].split("."))
    version = f"{major}.{int(minor) + 1}.0"
    path.write_text(version + "\n", encoding="utf-8")
    return version


def apply_proposal(proposal_dir, rules_dir) -> int:
    """Merge a proposal's ACCEPTED rules into `rules_dir`. This is the one
    sanctioned writer of LLM rules into a live rules dir -- explicit and
    human-invoked (or driven by the rebuild demo on its own scratch copy),
    never a side effect of propose. Validates the merged dir via load_rules
    before declaring success and bumps VERSION per §6.2."""
    proposal = json.loads((Path(proposal_dir) / "proposal.json").read_text(
        encoding="utf-8"))
    accepted = proposal["accepted"]
    if accepted:
        dest = Path(rules_dir) / "extraction.yaml"
        parsed = yaml.safe_load(dest.read_text(encoding="utf-8")) or {}
        parsed.setdefault("rules", {}).update(accepted)
        dest.write_text(yaml.safe_dump(parsed, sort_keys=True),
                        encoding="utf-8")
        bump_version(rules_dir)
        load_rules(rules_dir)   # CrossFileError here means do not proceed
    return len(accepted)


# --------------------------------------------------------------------------
# compare -- score each model's candidate rules with the eval harness
# --------------------------------------------------------------------------

# The headline metrics for the comparison table. Full metrics.jsonl for every
# leg lives in the per-model eval dirs; this list only curates the render.
_COMPARE_METRICS = (
    "extraction_recall", "extraction_precision", "top1_claim_accuracy",
    "propagated_value_accuracy", "pairwise_precision", "pairwise_recall",
    "false_merge_count", "false_split_count", "undecidable_rate",
)


def _metric_rows(eval_dir: Path) -> dict[str, float]:
    rows = {}
    with open(eval_dir / "metrics.jsonl", encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            if r["metric"] in _COMPARE_METRICS:
                rows[f"{r['metric']}/{r['scope']}"] = r["value"]
    return rows


def compare(proposal_dirs, *, observations_path, rules_dir="rules",
            labels_path="labels/labels.csv", out_dir) -> Path:
    """One baseline eval on the live rules, then one eval per model's
    candidate rules against that SAME baseline -- eval.py's four-bucket diff
    and LabelsMovedError guard apply unchanged, which is the point: the
    fitness function is the existing harness, not a second scorer."""
    from eval import evaluate  # deferred: keeps `propose` label-free

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    base_eval = evaluate(observations_path, rules_dir, labels_path,
                         out_dir / "baseline",
                         runs_root=out_dir / "baseline" / "runs")
    base_manifest = json.loads((base_eval / "eval_manifest.json").read_text(
        encoding="utf-8"))
    base_outcomes = json.loads((base_eval / "outcomes.json").read_text(
        encoding="utf-8"))

    comparison = {
        "baseline": {"eval_dir": str(base_eval),
                     "run_id": base_manifest["run_id"],
                     "labels_hash": base_manifest["labels_hash"],
                     "metrics": _metric_rows(base_eval)},
        "models": {},
    }
    for pdir in proposal_dirs:
        pdir = Path(pdir)
        proposal = json.loads((pdir / "proposal.json").read_text(
            encoding="utf-8"))
        model = proposal["model"]
        model_eval = evaluate(
            observations_path, pdir / "rules", labels_path, out_dir / model,
            runs_root=out_dir / model / "runs",
            baseline_outcomes=base_outcomes,
            baseline_labels_hash=base_manifest["labels_hash"],
            baseline_run_id=base_manifest["run_id"],
            changed_rule_files=("extraction.yaml",))
        comparison["models"][model] = {
            "eval_dir": str(model_eval),
            "proposal_dir": str(pdir),
            "accepted_rules": sorted(proposal["accepted"]),
            "rejected_count": len(proposal["rejected"]),
            "four_bucket": json.loads(
                (model_eval / "four_bucket.json").read_text(encoding="utf-8")),
            "metrics": _metric_rows(model_eval),
        }

    (out_dir / "comparison.json").write_text(
        json.dumps(comparison, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    (out_dir / "comparison.md").write_text(_render_comparison(comparison),
                                           encoding="utf-8")
    return out_dir


def _render_comparison(comparison: dict) -> str:
    models = sorted(comparison["models"])
    lines = ["# Rule-compiler model comparison", ""]
    lines.append("| model | rules accepted | fixed | broken | stable_correct "
                 "| stable_incorrect |")
    lines.append("|---|---|---|---|---|---|")
    for m in models:
        info = comparison["models"][m]
        fb = info["four_bucket"]
        lines.append(f"| {m} | {len(info['accepted_rules'])} | {fb['fixed']} "
                     f"| {fb['broken']} | {fb['stable_correct']} "
                     f"| {fb['stable_incorrect']} |")
    lines += ["", "| metric | baseline | " + " | ".join(models) + " |",
              "|---|---|" + "---|" * len(models)]
    keys = sorted(set(comparison["baseline"]["metrics"])
                  | {k for m in models
                     for k in comparison["models"][m]["metrics"]})
    for k in keys:
        cells = [str(comparison["baseline"]["metrics"].get(k, ""))]
        cells += [str(comparison["models"][m]["metrics"].get(k, ""))
                  for m in models]
        lines.append(f"| {k} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _print_trace_url(proposal_dir) -> None:
    proposal = json.loads((Path(proposal_dir) / "proposal.json").read_text(
        encoding="utf-8"))
    if proposal.get("trace_url"):
        print(f"trace: {proposal['trace_url']}")


def _usage() -> str:
    return (
        "usage:\n"
        "  python3 rule_compiler.py propose <run_dir> [--model ID]\n"
        "      [--observations obs.csv] [--rules-dir rules]\n"
        "      [--backlog no_extraction|payload_gaps]\n"
        "      [--out proposals] [--cache-dir llm_cache]\n"
        "  python3 rule_compiler.py compare <proposal_dir>... \n"
        "      [--observations obs.csv] [--rules-dir rules]\n"
        "      [--labels labels/labels.csv] [--out proposals/comparison]\n"
        "  python3 rule_compiler.py apply <proposal_dir> <rules_dir>\n"
    )


def main(argv: list[str]) -> int:
    if not argv:
        print(_usage(), file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]
    opts = {"--observations": "obs-data/observations.csv",
            "--rules-dir": "rules", "--labels": "labels/labels.csv",
            "--model": DEFAULT_MODEL, "--cache-dir": "llm_cache",
            "--backlog": "no_extraction", "--out": None}
    positional = []
    i = 0
    while i < len(rest):
        if rest[i] in opts:
            opts[rest[i]] = rest[i + 1]
            i += 2
        else:
            positional.append(rest[i])
            i += 1
    if cmd == "propose" and len(positional) == 1:
        import langfuse_sink
        out = propose(positional[0], opts["--model"],
                      observations_path=opts["--observations"],
                      rules_dir=opts["--rules-dir"],
                      out_root=opts["--out"] or "proposals",
                      cache_dir=opts["--cache-dir"],
                      backlog_mode=opts["--backlog"],
                      sink=(langfuse_sink.emit_tool_trace
                            if langfuse_sink.enabled() else None))
        print(out)
        _print_trace_url(out)
        return 0
    if cmd == "apply" and len(positional) == 2:
        applied = apply_proposal(positional[0], positional[1])
        print(f"applied {applied} rules to {positional[1]}")
        return 0
    if cmd == "compare" and positional:
        out = compare(positional,
                      observations_path=opts["--observations"],
                      rules_dir=opts["--rules-dir"],
                      labels_path=opts["--labels"],
                      out_dir=opts["--out"] or "proposals/comparison")
        print(out)
        return 0
    print(_usage(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
