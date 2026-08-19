# Demo runbook — "the pipeline improves itself, refereed by its own harness"

Six acts, ~15 minutes. Every command below was rehearsed end-to-end; the
expected output blocks are real captures. All model responses are
pin-cached in `llm_cache/`, so the live demo replays them byte-identically
— no on-stage API risk, no nondeterminism. Keep a Langfuse tab open; every
LLM step prints a public trace URL you can click live.

The arc: start at an honest baseline that names its own gaps → watch the
safety gate refuse a model's patch → discover the refusal was the *ground
truth's* fault → fix the yardstick through the blind adjudication path →
re-run and watch the same gate approve → finish by rebuilding the entire
ruleset from nothing.

---

## Architecture overview (show this before Act 0)

Slide versions (open in draw.io): `demo-architecture.drawio` — the
system-level view below; `demo-dataflow.drawio` — the code-level companion
showing how data moves module to module (observation rows → Extractions →
scored Claims → the invariant-#1 fork into identity-only clustering vs
field resolution → confidence → bundle), with the Tracer lane underneath
and the offline consumers reading the written bundle.

```text
                        DETERMINISTIC RUNTIME (no LLM, no labels — invariant #5)
 obs-data/            ┌──────────────────────────────────────────────────────┐
 observations.csv ──► │ extract ► normalize ► claims ► score ► cluster ►     │
                      │ resolve fields ► confidence/stability                │
 rules/  ───────────► │   (7 versioned YAML files, hash-policed VERSION)     │
                      └──────────────┬───────────────────────────────────────┘
                                     ▼
                      runs/<run_id>/  run bundle
                        final-output.csv   ◄─ consumer view (banded confidence)
                        resolutions/entities/claims.csv, trace.jsonl, REPORT.md
                        every value replays to a payload span (replay.py)
                                     │
              ┌──────────────────────┼──────────────────────────┐
              ▼                      ▼                          ▼
   EVAL HARNESS (offline)   FAILURE QUEUES (in the bundle)   Langfuse traces
   eval.py vs labels/       no_extraction / payload gaps     (public, per
   four-bucket diff:        vocab rejects, merge refusals,    LLM call)
   fixed / broken /         low confidence
   stable_correct/incorrect          │
              ▲                      ▼
              │             LLM SIDECAR (offline, pin-cached, --model)
              │             rule_compiler / alias_miner / label_qa /
              │             rule_copilot / merge_assistant / narrate
              │                      │  proposals only (origin: llm_proposed)
              │                      ▼
              └──── gate: surfaced only if fixed>0 AND broken==0
                                     │  human-invoked `apply`
                                     ▼
                        rules/ updated + VERSION bump  ──► next run improves
```

Four things to say over this diagram, because every act depends on them:

1. **The serving path is deterministic and model-free.** Same input + same
   rules = byte-identical output; no LLM and no label is reachable from
   `run.py` (structural, test-enforced). LLMs live entirely in the offline
   sidecar.
2. **The pipeline names its own gaps.** The run bundle carries metered
   failure queues (unmatched payloads, vocabulary rejects, refused merges,
   low confidence) — every LLM tool in this demo consumes a queue, never a
   hunch.
3. **The eval harness is the referee.** Labels are versioned ground truth
   kept structurally independent of the pipeline; every proposed change is
   scored as a per-label four-bucket diff (fixed / broken / stable), and
   model diffs surface only through the fixed>0 / broken==0 gate.
4. **Model output becomes rules, not answers.** Proposals are validated
   (values must come from real payload spans), gated, and merged by a
   human-invoked `apply` that re-validates and bumps the rules VERSION —
   so every improvement lands as versioned, auditable YAML, and the
   runtime stays cheap and reproducible forever.

---

## Act 0 — Reset to the unoptimized baseline

What "unoptimized baseline" means here: the committed `rules/` (missing
aliases and rules by design), the committed `labels/` at 0.1.0 (still
carrying its four seeded errors — they are Act 3's material), and **no**
build outputs. Clean exactly this:

```bash
rm -rf runs evals adjudication proposals returned.csv   # build outputs; also
                                                        # resets the trend store +
                                                        # version-state baselines
git status --short rules labels     # MUST already be clean here the first time;
                                    # after a previous demo, the next line
                                    # reverts that demo's applies
git checkout -- rules labels
cp -r rules rules-baseline          # snapshot for the Act 6 finale
```

> ⚠️ `git checkout -- rules labels` restores the **committed** state — run
> the demo only from a committed tree. (On this branch before its first
> commit, that command would also strip the uncommitted
> `field_resolution.yaml#final_output` block and the pipeline would refuse
> to load.)

**Keep `llm_cache/`** — it is what makes the demo deterministic and free.
Delete it only if you deliberately want live model calls (slower, small
cost, answers may vary). Keep `.env` (API + Langfuse keys).

---

## Act 1 — Baseline, and a pipeline that names its own gaps (2 min)

```bash
RUN=$(python3 run.py)
EVAL=$(python3 eval.py | tail -1)
head -4 "$RUN/final-output.csv"
```

Expected:

```text
obs_id,vendor,model,device_type,firmware,entity_id,confidence
OBS-001,Axis Communications,P3245-LVE,ip_camera,10.12.114,E-dccf99f031f3,medium
OBS-002,Axis Communications,P3245-LVE,ip_camera,10.12.114,E-dccf99f031f3,medium
OBS-003,Axis Communications,P3245-LVE,ip_camera,,E-dccf99f031f3,medium
```

Talking points: `final-output.csv` is the consumer view — banded
confidence, firmware blank on OBS-003 because *that observation* never
witnessed it. Then the honest numbers: vendor top1_claim_accuracy
**0.643**, **54** incorrect outcomes, and `wisenet` sitting in the
vocab-reject queue
(`grep '"op":"vocab_reject"' "$RUN/trace.jsonl" | grep wisenet | head -1`). Every
improvement that follows is pulled from a queue this baseline run
produced.

Files & code:

- `run.py:run_pipeline` — the stage flow; `obs_pipeline/loader.py:load_rules`
  (cross-file validation, hash rollup), then `extract.py` → `normalize.py` →
  `claims.py` → `scoring.py:score` → `entity.py:resolve_entities` →
  `fields.py` → `confidence.py`, all traced via `trace.py:Tracer.step`
- `obs_pipeline/bundle.py:write_bundle` — writes the bundle incl.
  `final-output.csv` (banding + direct-only rules from
  `rules/field_resolution.yaml#final_output`)
- `eval.py:evaluate` / `score_against_labels` — scores the bundle against
  `labels/labels.csv` (loaded via `label_tools.py:load_labels`), metric
  registry `metrics.yaml`
- Inputs: `obs-data/observations.csv`, `rules/*.yaml`, `rules/canonical_vocab.csv`

## Act 2 — The gate that says no (2 min)

```bash
python3 rule_copilot.py "$EVAL" --model claude-sonnet-5
tail -3 proposals/copilot/claude-sonnet-5/gate/*/regressions.csv
```

Expected:

```text
surfaced=False four_bucket={'broken': 2, 'fixed': 37, 'stable_correct': 220, 'stable_incorrect': 17}
OBS-038,field,device_type,ip_camera,nvr,extraction.yaml|normalization.yaml
OBS-038,field,firmware,4.30.085,V4.30.085,extraction.yaml|normalization.yaml
```

Talking points: the model proposed patches fixing **37 of 54** misses —
and the harness still refused to surface the diff, because 2 cases broke.
Leave the mystery hanging: both breaks are on OBS-038. Nothing
model-generated reaches the rules without proving it breaks nothing.

Files & code:

- `rule_copilot.py:collect_targets` (the eval's incorrect outcomes),
  `build_prompt` (targets + observations + full rule-file text), `propose`
- `llm_client.py:call_model` — pin-and-cache (this act is a cache hit),
  `request_body` structured-output schema
- Validation: `rule_compiler.py:validate_rule`,
  `alias_miner.py:validate_alias`; candidate rules via
  `rule_compiler.py:write_candidate_rules`
- The gate: `eval.py:evaluate` with `baseline_outcomes` →
  `four_bucket_diff`; verdict + regressions in
  `proposals/copilot/claude-sonnet-5/{proposal.json,gate/}`
- Telemetry: `langfuse_sink.py:emit_tool_trace`

## Act 3 — The yardstick was wrong; fix it blind (3 min)

```bash
python3 label_qa.py --model claude-sonnet-5
column -s, -t proposals/labels/claude-sonnet-5/label_suspects.csv | cut -c1-120
python3 adjudicate.py "$RUN" --reopen proposals/labels/claude-sonnet-5/label_suspects.csv
```

The QA sweep flags 6 suspects with quoted payload evidence — including
OBS-038, labeled `ip_camera` while its banner says "16-channel Network
Video Recorder". The reviewer accepts the 4 high-confidence ones,
re-labeling **blind** from the packet's evidence columns (scripted here as
the reviewer's keyboard):

```bash
python3 - "adjudication/$(basename "$RUN")/packet.csv" <<'EOF'
import csv, sys
packet = {r["obs_id"]: r for r in csv.DictReader(open(sys.argv[1]))}
suspects = [s for s in csv.DictReader(
    open("proposals/labels/claude-sonnet-5/label_suspects.csv"))
    if s["llm_confidence"] == "high"]
with open("returned.csv", "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["obs_id", "key_type", "key", "value", "obs_hash", "labeled_by"])
    for s in suspects:
        w.writerow([s["obs_id"], "field", s["key"], s["proposed_value"],
                    packet[s["obs_id"]]["obs_hash"], "demo-reviewer"])
EOF
python3 adjudicate.py "$RUN" returned.csv
```

Expected:

```text
applied   OBS-038.device_type 'ip_camera' -> 'nvr' (value_changed)
applied   OBS-039.model 'Q6315-LE' -> 'Q6135-LE' (value_changed)
applied   OBS-040.vendor 'Bosch Security Systems' -> 'Vivotek' (value_changed)
applied   OBS-041.firmware '8.12.2200' -> '8.12.0022' (value_changed)
4 admissible, 4 applied, 0 not applied
labels 0.1.0 -> 0.2.0 (minor)
archived labels/archive/0.1.0-labels.csv
```

Talking points: the LLM *nominates*, never writes — corrections flow
through the same blind adjudication path as any label, the old set is
archived, the version bumps from measured change. (The `--reopen` flag
exists because these labels were high-certainty: the sweep finds errors
exactly where the process assumed labels were settled.) The 2 medium-
confidence suspects are deliberately left for a human pass — show that
restraint.

Files & code:

- `label_qa.py:sweep` / `build_prompt` (labels + payload evidence, no
  pipeline output — test-enforced) / `validate_suspect`; output
  `proposals/labels/claude-sonnet-5/label_suspects.csv`
- `adjudicate.py:export_packet` with `reopen_obs_ids` (evidence-only
  packet: `adjudication/<run_id>/packet.csv`),
  `import_returned_labels` (admissibility: packet membership, declared
  keys, live `obs_hash`)
- `label_tools.py:apply_adjudicated` — the only sanctioned label writer:
  §7.2.2 precedence, `propose_label_bump`, archive + `journal.jsonl`
- Mutated: `labels/labels.csv`, `labels/VERSION`,
  `labels/archive/0.1.0-labels.csv`

## Act 4 — Alias mining, with a model bake-off (3 min)

```bash
python3 alias_miner.py "$RUN" --model claude-sonnet-5
python3 alias_miner.py "$RUN" --model claude-haiku-4-5
python3 rule_compiler.py compare proposals/aliases/claude-sonnet-5 \
    proposals/aliases/claude-haiku-4-5 --out proposals/aliases/comparison
grep -E 'top1_claim_accuracy/field:vendor|^\| (model|claude)' \
    proposals/aliases/comparison/comparison.md
python3 alias_miner.py apply proposals/aliases/claude-sonnet-5 rules
```

Expected:

```text
| claude-haiku-4-5 | 10 | 0 | 0 | 224 | 52 |
| claude-sonnet-5  |  9 | 0 | 0 | 224 | 52 |
| top1_claim_accuracy/field:vendor | 0.657143 | 0.842857 | 0.828571 |
applied 9 aliases to rules      # rules VERSION -> 0.3.0
```

Talking points: both models recover the deliberately-missing aliases
(`wisenet`, `amcrest`, …); vendor top1 jumps **0.657 → ~0.84** with zero
broken; two models scored through the *same* eval harness, side by side —
and cheap haiku edges sonnet here, which is the point of measuring instead
of assuming. `apply` is the sanctioned merge: validates the merged rules,
bumps VERSION.

Files & code:

- `alias_miner.py:collect_backlog` (out-of-vocab claim values from
  `claims.csv`'s `in_vocab` column) / `build_prompt` / `validate_alias`
  (target must exist in `rules/canonical_vocab.csv`; surface must be in
  `normalize.py:_surface_key` lookup form and observed in the backlog)
- `rule_compiler.py:compare` — baseline + per-model eval through
  `eval.py:evaluate`; renders
  `proposals/aliases/comparison/comparison.md`
- `alias_miner.py:apply_aliases` + `rule_compiler.py:bump_version` —
  merges into `rules/normalization.yaml`, re-validates via
  `loader.py:load_rules`
- Mutated: `rules/normalization.yaml`, `rules/VERSION` (→ 0.3.0)

## Act 5 — The same gate says yes (3 min)

```bash
EVAL2=$(python3 eval.py | tail -1)
python3 rule_copilot.py "$EVAL2" --model claude-sonnet-5
python3 rule_copilot.py apply proposals/copilot/claude-sonnet-5 rules
python3 eval.py | tail -1     # final numbers
```

Expected:

```text
surfaced=True four_bucket={'broken': 0, 'fixed': 38, 'stable_correct': 224, 'stable_incorrect': 14}
applied 39 patches to rules   # rules VERSION -> 0.4.0
```

Final eval: incorrect outcomes **54 → 14**; extraction recall — firmware
**1.00** (from 0.81), model **0.90** (from 0.65), vendor **0.96**,
device_type **0.82**. Talking points: same tool, same gate as Act 2 — the
difference is a corrected yardstick and better normalization. `apply`
refuses ungated diffs by construction. The improvement loop is: queue →
propose → validate → gate → human-invoked apply → version bump, every step
on screen.

Files & code:

- Same propose/gate path as Act 2 (`rule_copilot.py:propose` →
  `eval.py:evaluate` → `four_bucket_diff`), now against the corrected
  `labels/labels.csv` (0.2.0) and alias-improved `rules/`
- `rule_copilot.py:apply_patches` — splits the mixed diff (`rule:` keys →
  `rules/extraction.yaml`, `alias:` keys → `rules/normalization.yaml`),
  refuses if `surfaced` is false, one `bump_version` + `load_rules`
  re-validation
- Mutated: `rules/extraction.yaml`, `rules/normalization.yaml`,
  `rules/VERSION` (→ 0.4.0)

## Act 6 — Finale: rebuild the whole ruleset from nothing (3 min)

```bash
python3 rebuild_demo.py --model claude-sonnet-5 --rounds 3 --rules-dir rules-baseline
sed -n '8,13p' proposals/rebuild-demo/demo_report.md
```

Expected:

```text
| round | backlog | rules applied | aliases applied | fixed | broken |
| 0 | — | 0 | 0 | — | — |
| 1 | 74 | 41 | 13 | 104 | 0 |
| 2 | 6 | 9 | 3 | 6 | 0 |
| 3 | 3 | 0 | 0 | 0 | 0 |
```

Talking points: ablate every regex rule and alias, keep only the
structural skeleton — three propose→apply→eval rounds rebuild it all:
**104 label outcomes fixed, zero broken, converged, ~$0.35** — and the
rebuilt ruleset beats the original hand-written baseline on three of four
fields. (Note `fixed=104, broken=0`: on the original faulty labels this
same run showed `broken=1`… which was OBS-038. Every "regression" this
system ever reported traced to that one bad label — now fixed.)

Files & code:

- `rebuild_demo.py:ablate` — strips `extraction.yaml#rules` and all alias
  maps from a copy of `rules-baseline/`, keeps structured lifts / OUI /
  port signatures; `run_demo` drives the rounds
- Per round: `run.py:run_pipeline` →
  `rule_compiler.py:collect_backlog(mode="payload_gaps")` (observations
  whose `raw_payload[start:end]` evidence spans never fired) → `propose`
  → `apply_proposal`; then `alias_miner.py:propose` → `apply_aliases`;
  then `eval.py:evaluate` chained on the previous round's outcomes
- Outputs: `proposals/rebuild-demo/demo_report.md`, `demo.json`, and the
  evolving scratch rules dir `proposals/rebuild-demo/rules-ablated/`

---

## Closing line

Nothing in the serving path calls a model. Everything a model proposed
arrived as versioned, validated, regression-gated YAML a human applied —
and every claim in this demo is a public Langfuse trace or a replayable
run bundle you can inspect afterwards.

## Timing / cost summary

| Act | Wall time (cached) | API cost (cached) |
| --- | --- | --- |
| 1 baseline | ~5 s | $0 |
| 2 gate refuses | ~10 s | $0 |
| 3 label fix | ~15 s | $0 |
| 4 aliases | ~30 s (3 evals) | $0 |
| 5 gate approves | ~30 s | $0 |
| 6 rebuild | ~60 s (7 pipeline runs + 4 evals) | $0 |

First-ever run (cold cache) adds ~6 sonnet + 1 haiku calls ≈ $1–2 total.
