#!/usr/bin/env python3
"""Rules-rebuild demo: ablate the hand-written extraction knowledge, then
let the LLM tools build it back and chart the recovery.

What gets ablated (into a scratch rules dir -- the live rules/ is never
touched): every regex rule in extraction.yaml and every alias map entry in
normalization.yaml. What stays: structured column lifts, the OUI map, port
signatures, and all scoring/resolution config -- the skeleton that keeps
clustering alive so the demo isolates the EXTRACTION knowledge the LLM must
recover.

Each round then runs the real loop the tools were built for:

    run pipeline -> rule_compiler.propose (payload_gaps backlog) -> apply
                 -> run pipeline -> alias_miner.propose -> apply
                 -> eval vs labels (four-bucket against the previous round)

and the report charts accuracy (extraction_recall, top1_claim_accuracy)
and coverage (field_fill_rate, known_rate, no_extraction_rate) per round,
with fixed/broken counts and a Langfuse trace per LLM call. apply here is
the same sanctioned rule_compiler.apply_proposal / alias_miner.apply_aliases
merge a human would invoke -- the demo automates the invocation on its own
scratch copy, not the judgement of merging into rules/.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import yaml

import alias_miner
import rule_compiler
from llm_client import DEFAULT_MODEL
from obs_pipeline.loader import RULE_FILES, VOCAB_FILE, load_rules
from run import run_pipeline

_ACCURACY = ("extraction_recall", "extraction_precision",
             "top1_claim_accuracy")
_COVERAGE = ("field_fill_rate", "known_rate", "no_extraction_rate")


def ablate(rules_dir, dest) -> Path:
    """A degraded copy of the rules: no regex extraction, no aliases."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    src = Path(rules_dir)
    for name in (*RULE_FILES, VOCAB_FILE):
        shutil.copy(src / name, dest / name)

    extraction = yaml.safe_load((dest / "extraction.yaml").read_text(
        encoding="utf-8")) or {}
    extraction["rules"] = {}
    (dest / "extraction.yaml").write_text(
        yaml.safe_dump(extraction, sort_keys=True), encoding="utf-8")

    normalization = yaml.safe_load((dest / "normalization.yaml").read_text(
        encoding="utf-8")) or {}
    for block in ("vendor_alias", "device_type_alias", "oem_rebrand"):
        if block in normalization:
            normalization[block]["map"] = {}
    (dest / "normalization.yaml").write_text(
        yaml.safe_dump(normalization, sort_keys=True), encoding="utf-8")

    (dest / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    load_rules(dest)   # the ablated dir must still be a valid rule state
    return dest


def _eval_metrics(eval_dir: Path, runs_root: Path) -> dict[str, float]:
    """Accuracy metrics from the eval bundle plus coverage metrics from the
    run bundle it scored (field vocabulary read from each bundle itself,
    never hardcoded)."""
    out: dict[str, float] = {}
    with open(eval_dir / "metrics.jsonl", encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            if r["metric"] in _ACCURACY:
                out[f"{r['metric']}/{r['scope']}"] = r["value"]
    manifest = json.loads((eval_dir / "eval_manifest.json").read_text(
        encoding="utf-8"))
    run_metrics = Path(runs_root) / manifest["run_id"] / "metrics.jsonl"
    with open(run_metrics, encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            if r["metric"] in _COVERAGE:
                out[f"{r['metric']}/{r['scope']}"] = r["value"]
    return out


def run_demo(observations_path="obs-data/observations.csv",
             rules_dir="rules", labels_path="labels/labels.csv",
             model=DEFAULT_MODEL, rounds=3, *,
             out_root="proposals/rebuild-demo", cache_dir="llm_cache",
             transport=None, sink=None) -> Path:
    from eval import evaluate

    out = Path(out_root)
    out.mkdir(parents=True, exist_ok=True)
    work = ablate(rules_dir, out / "rules-ablated")

    history: list[dict] = []
    prev_outcomes, prev_hash, prev_run_id = None, None, None

    for rnd in range(rounds + 1):   # round 0 = ablated baseline, no LLM
        entry = {"round": rnd, "rules_applied": 0, "aliases_applied": 0,
                 "trace_urls": []}
        if rnd > 0:
            run_dir = run_pipeline(observations_path, work,
                                   out / f"round{rnd}" / "runs-pre")
            proposal = rule_compiler.propose(
                run_dir, model, observations_path=observations_path,
                rules_dir=work, out_root=out / f"round{rnd}" / "rules",
                cache_dir=cache_dir, backlog_mode="payload_gaps",
                transport=transport, sink=sink)
            entry["rules_applied"] = rule_compiler.apply_proposal(
                proposal, work)
            p = json.loads((proposal / "proposal.json").read_text(
                encoding="utf-8"))
            entry["backlog"] = len(p["backlog"])
            if p.get("trace_url"):
                entry["trace_urls"].append(p["trace_url"])

            # Fresh mini-run under the just-applied rules, so alias mining
            # sees the out-of-vocab values the NEW rules produce.
            run_dir2 = run_pipeline(observations_path, work,
                                    out / f"round{rnd}" / "runs-mid")
            aliases = alias_miner.propose(
                run_dir2, model, rules_dir=work,
                out_root=out / f"round{rnd}" / "aliases",
                cache_dir=cache_dir, transport=transport, sink=sink)
            entry["aliases_applied"] = alias_miner.apply_aliases(aliases, work)
            a = json.loads((aliases / "proposal.json").read_text(
                encoding="utf-8"))
            if a.get("trace_url"):
                entry["trace_urls"].append(a["trace_url"])

        eval_dir = evaluate(
            observations_path, work, labels_path, out / f"round{rnd}" / "eval",
            runs_root=out / f"round{rnd}" / "eval" / "runs",
            baseline_outcomes=prev_outcomes,
            baseline_labels_hash=prev_hash, baseline_run_id=prev_run_id)
        entry["metrics"] = _eval_metrics(eval_dir,
                                         out / f"round{rnd}" / "eval" / "runs")
        if (eval_dir / "four_bucket.json").exists():
            entry["four_bucket"] = json.loads(
                (eval_dir / "four_bucket.json").read_text(encoding="utf-8"))
        manifest = json.loads((eval_dir / "eval_manifest.json").read_text(
            encoding="utf-8"))
        prev_outcomes = json.loads((eval_dir / "outcomes.json").read_text(
            encoding="utf-8"))
        prev_hash, prev_run_id = manifest["labels_hash"], manifest["run_id"]
        history.append(entry)

    result = {"model": model, "rounds": rounds, "history": history,
              "rules_dir": str(work)}
    (out / "demo.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "demo_report.md").write_text(_render(result), encoding="utf-8")
    return out


def _render(result: dict) -> str:
    history = result["history"]
    lines = [
        "# Rules-rebuild demo",
        "",
        f"Model: `{result['model']}`. Round 0 is the ablated baseline (no",
        "regex extraction rules, no aliases); each later round is one",
        "propose→apply cycle of rule_compiler (payload_gaps backlog) plus",
        "alias_miner, scored by the eval harness against frozen labels.",
        "",
        "| round | backlog | rules applied | aliases applied | fixed "
        "| broken |",
        "|---|---|---|---|---|---|",
    ]
    for h in history:
        fb = h.get("four_bucket") or {}
        lines.append(
            f"| {h['round']} | {h.get('backlog', '—')} "
            f"| {h['rules_applied']} | {h['aliases_applied']} "
            f"| {fb.get('fixed', '—')} | {fb.get('broken', '—')} |")

    keys = sorted({k for h in history for k in h["metrics"]})
    header = " | ".join(f"round {h['round']}" for h in history)
    lines += ["", f"| metric | {header} |",
              "|---|" + "---|" * len(history)]
    for k in keys:
        cells = " | ".join(str(h["metrics"].get(k, "")) for h in history)
        lines.append(f"| {k} | {cells} |")

    lines += ["", "## Traces", ""]
    for h in history:
        for url in h["trace_urls"]:
            lines.append(f"- round {h['round']}: {url}")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    opts = {"--model": DEFAULT_MODEL, "--rounds": "3",
            "--observations": "obs-data/observations.csv",
            "--rules-dir": "rules", "--labels": "labels/labels.csv",
            "--out": "proposals/rebuild-demo", "--cache-dir": "llm_cache"}
    i = 0
    while i < len(argv):
        if argv[i] in opts:
            opts[argv[i]] = argv[i + 1]
            i += 2
        else:
            print("usage: python3 rebuild_demo.py [--model ID] [--rounds N] "
                  "[--observations PATH] [--rules-dir rules] "
                  "[--labels PATH] [--out proposals/rebuild-demo] "
                  "[--cache-dir llm_cache]", file=sys.stderr)
            return 2
    import langfuse_sink
    out = run_demo(opts["--observations"], opts["--rules-dir"],
                   opts["--labels"], opts["--model"],
                   rounds=int(opts["--rounds"]), out_root=opts["--out"],
                   cache_dir=opts["--cache-dir"],
                   sink=(langfuse_sink.emit_tool_trace
                         if langfuse_sink.enabled() else None))
    print(out / "demo_report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
