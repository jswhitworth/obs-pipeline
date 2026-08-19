# obs-pipeline

A deterministic, auditable pipeline that resolves raw network-device
observations — ONVIF, SNMP `sysDescr`, HTTP banners, mDNS TXT, telnet
banners — into entity records with `vendor`, `model`, `device_type`,
`firmware`, `entity_id`, and `confidence`. Every value in the output traces
back to a specific observation and rule through a content-addressed
derivation log, and that trace can be replayed independently of the engine
that produced it.

On top of the deterministic runtime sits an offline layer of six LLM tools
that propose rules, mine vocabulary aliases, audit ground-truth labels, and
narrate results — all outside the runtime path, all advisory, all merged by
a human.

```
python3 run.py                # → runs/<run_id>/   74 observations → 68 entities
python3 -m pytest -q          # → 343 passed in ~7s
```

## Why this exists

Fingerprinting a device fleet from heterogeneous, contradictory network
observations is easy to get *approximately* right and very hard to get
*accountably* right. This repo optimizes for the second thing: every
resolved field, every cluster, every confidence score is reconstructible
from a `trace.jsonl` file alone, without re-running any scoring code. That
guarantee is enforced mechanically (`replay.py`, `tests/test_replay.py`),
not by convention.

## Quickstart

Requires **Python 3.11** and **PyYAML** — nothing else for the pipeline
itself.

```bash
pip install pyyaml

# Run the pipeline over the sample fleet
python3 run.py                              # obs-data/observations.csv → runs/<run_id>/
python3 run.py <obs.csv> <rules_dir> <out_root>

# Score a run against ground truth (four-bucket diff: fixed/broken/stable_correct/stable_incorrect)
python3 eval.py                             # → evals/<eval_id>/

# Prove every output value has a derivation, independent of the engine
python3 replay.py runs/<run_id>             # exit 1 on any hole

# Full test suite
python3 -m pytest -q                        # 343 tests, ~7s
```

A run produces `runs/<run_id>/`: `manifest.json` (rule version + hashes +
engine commit), `trace.jsonl` (the derivation DAG), `claims.csv`,
`membership.csv`, `entities.csv`, `resolutions.csv` (full-fidelity,
per-field provenance), `final-output.csv` (the consumer-facing projection),
and a regenerated `REPORT.md`.

<details>
<summary>Real output, from this repo's sample data</summary>

```
Observations: 74
Entities:     68
Singletons:   63 (93%)

Field       Known  Unknown  Undecidable  Mean confidence
vendor      63     5        0            0.67
model       49     19       0            0.54
firmware    46     21       1            0.52
device_type 53     15       0            0.19
```

`REPORT.md` also ranks vocabulary rejects — out-of-vocab values grouped by
field with the observations that produced them — which is exactly the
backlog the LLM alias-mining tool below consumes.
</details>

## Architecture

```
load_rules ──► load_observations ──► build_claims ──► resolve_entities
                                                            │
                                     observation_fields ◄── resolve_fields
                                                            │
                    entity_confidence + stability ──► write_bundle ──► emit_metrics ──► write_report
```

Each stage is one module in `obs_pipeline/`, mapped one-to-one onto the
pipeline's seven rule categories — each with its own YAML rule file:

| Stage | Module | Rule category | Rule file |
|---|---|---|---|
| Extraction | `extract.py` | 1 | `rules/extraction.yaml` |
| Normalization | `normalize.py` | 2 | `rules/normalization.yaml` |
| Claim construction | `claims.py` | 3 | `rules/claims.yaml` |
| Scoring (shared) | `scoring.py` | 4 | `rules/scoring.yaml` |
| Entity resolution | `entity.py` | 5 + 6 | `rules/entity_resolution.yaml` |
| Field resolution | `fields.py` | 7 | `rules/field_resolution.yaml` |
| Value domains | `vocab.py` | — | `rules/canonical_vocab.csv` |

Supporting modules: `loader.py` (parses + cross-file-validates + hashes the
rule set), `trace.py` (the derivation DAG), `bundle.py` (run bundle
writers), `metrics.py` (label-free metrics), `confidence.py`, `report.py`.

### Design invariants

These hold structurally, not by convention — several are enforced by tests
or by the loader raising `CrossFileError` at init:

1. **DAG discipline** — entity resolution reads identity claims only, never
   resolved field values. No field → cluster feedback loop.
2. **Canonical keys** — `field` and `link_basis` are fixed vocabularies;
   `source` is metadata *on* a claim, never part of the key.
3. **Scoring parity** — one scoring function
   (`independence_bonus(k) = b·(1 − rᵏ⁻¹)`, saturating corroboration bonus
   minus a capped conflict penalty), shared by claim construction and
   entity resolution. Coefficients differ per claim type; the formula
   shape never does.
4. **Rule-state traceability** — every output binds to an exact rule state
   via `run_id → manifest.json` (version, per-file hashes, a rollup hash,
   and the engine's git commit — suffixed `-dirty` if uncommitted code ran).
5. **Labels never enter the runtime path** — `run.py` and everything it
   imports have no code path that reads `labels/`.
6. **Pipeline output never enters hard-stratum label creation** — the
   mirror of #5: adjudication may *select* cases using pipeline output, but
   *presentation* to the human labeler must stay blind to it.
7. **No output value without a derivation** — enforced mechanically by
   `replay.py`, which reconstructs `claims.csv`, `membership.csv`, and
   `entities.csv` from `trace.jsonl` alone, without importing the engine.

Three confidence-combining operations exist, deliberately not conflated:
same value witnessed twice → max + saturating bonus (`scoring.py`); a
propagation chain → multiplicative (`fields.py`); different fields into one
record → harmonic mean (`confidence.py`).

## Rule and label versioning

`rules/` (currently `VERSION` **0.2.0**) and `labels/` (currently `VERSION`
**0.1.0**) are versioned independently, by the same mechanism pointed in
opposite directions: a canonicalized content hash of every file rolls up
into one hash, stored beside the version. If the rollup moves but the
`VERSION` file doesn't, `version_verified` goes `false` in the manifest —
"the hash polices the version," so a silent rule edit can't hide behind an
unbumped version number.

- `eval.py`'s `propose_bump` derives the rules-side bump from *measured*
  behavior (vocabulary narrowing → major, outcomes changed → minor, neutral
  → patch) — advisory, not authoritative.
- `label_tools.py`'s `apply_adjudicated` is the only sanctioned writer of
  adjudicated labels; it archives the replaced set to
  `labels/archive/<old-version>-labels.csv` and appends to
  `labels/journal.jsonl`.
- `adjudicate.py` runs the human-in-the-loop label pipeline: export a blind
  packet from a run, collect returned judgments, validate + apply them.
  Merge conflicts follow the *existing* basis-precedence rule
  (`§7.2.2`) rather than a second ad hoc merge rule; two tied, disagreeing
  tiers are refused back to a human rather than auto-resolved.

```bash
python3 label_tools.py                              # wide labels-initial.csv → long labels.csv
python3 adjudicate.py runs/<run_id>                  # → adjudication/<run_id>/packet.csv
python3 adjudicate.py runs/<run_id> returned.csv     # validate + apply adjudicated labels
python3 adjudicate.py runs/<run_id> --reopen suspects.csv   # re-queue label-QA suspects
```

## LLM tooling (offline, advisory, human-merged)

Six tools sit **outside** the deterministic runtime and read *written* run
bundles — `obs_pipeline/` never imports them, and `tests/test_rule_compiler.py`
enforces that the LLM plumbing never leaks into the runtime path. Every
tool pins its model call to a content hash and caches the response
(`llm_client.py`), so reruns over identical input are byte-identical and a
model or prompt change is an explicit, diffable event. Nothing here writes
directly to `rules/`, `labels/`, or entity resolution — every tool produces
a proposal that a human applies.

| Tool | Purpose | Output |
|---|---|---|
| `rule_compiler.py` | Turns the `no_extraction` backlog into candidate extraction/normalization rules; compares proposals model-vs-model through the eval harness | `proposals/<model>/` |
| `alias_miner.py` | Mines out-of-vocab claim values for existing-vocabulary aliases (`"wisenet"` → `Hanwha Vision`); flags genuinely new vendors as advisory-only | `proposed normalization.yaml` diff |
| `label_qa.py` | Audits ground truth against its own source payloads and against itself (contradicting `same_device` pairs) | `label_suspects.csv` |
| `rule_copilot.py` | Reads an eval miss, drafts a rule-diff fix, and gates its own suggestion on the eval harness (`fixed > 0 and broken == 0`) | surfaced patch, or an auditable non-surfaced miss |
| `merge_assistant.py` | Reviews `merge_refused` pairs — cases below the clustering weight threshold — with the same evidence a human reviewer would see | `merge_verdicts.csv` (advisory) |
| `narrate.py` | Explains low-confidence entities, narrates run-diffs against an eval, and spots anomalies metrics don't encode | `NARRATIVE.md` beside `REPORT.md` |

```bash
python3 rule_compiler.py propose runs/<run_id> --model claude-sonnet-5
python3 rule_compiler.py compare proposals/<m1> proposals/<m2>
python3 rule_compiler.py apply proposals/<m>/ <rules_dir>

python3 alias_miner.py runs/<run_id> --model <id>
python3 alias_miner.py apply proposals/aliases/<m> <rules_dir>

python3 rebuild_demo.py --model <id> --rounds 3     # ablate regex rules + aliases, watch the LLM rebuild them
python3 label_qa.py --model <id>                    # → label_suspects.csv (never overwrites labels/)
python3 rule_copilot.py evals/<eval_id> --model <id>
python3 rule_copilot.py apply proposals/copilot/<m> <rules_dir>   # refuses ungated diffs
python3 merge_assistant.py runs/<run_id> --model <id>
python3 narrate.py runs/<run_id> [--eval evals/<id>]
```

### Model access

`llm_client.py` talks to the Anthropic Messages API over stdlib `urllib`
(no SDK dependency), resolving credentials in order: `ANTHROPIC_API_KEY`
env var → `ANTHROPIC_AUTH_TOKEN` env var → a repo-root `.env` file → the
`ant` CLI's stored profile. Default model is `claude-sonnet-5`
(`--model claude-haiku-4-5` for cheap comparisons).

With `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL`
set (env or `.env`), every tool run that calls a model emits one **public**
Langfuse trace — flip that before pointing these tools at non-synthetic
data. The pipeline itself is never instrumented this way; `trace.jsonl`
stays the sole source of truth.

```bash
# .env (repo root, gitignored — never commit this file)
ANTHROPIC_API_KEY=sk-ant-...
LANGFUSE_PUBLIC_KEY=pk-lf-...      # optional
LANGFUSE_SECRET_KEY=sk-lf-...      # optional
LANGFUSE_BASE_URL=https://us.cloud.langfuse.com   # optional
```

## Repository layout

```
obs_pipeline/        deterministic engine — the only code run.py imports
run.py                pipeline entry point
eval.py               black-box eval harness (never imported by run.py)
replay.py             trace-only reconstruction, proves invariant #7
label_tools.py        label import/versioning (imports only loader + vocab)
adjudicate.py         blind human adjudication packets

rules/                7 rule YAML files + canonical_vocab.csv + VERSION
labels/               ground-truth labels + VERSION (checked in)
obs-data/             sample observation set (74 rows, 5 sources)

rule_compiler.py, alias_miner.py, label_qa.py,
rule_copilot.py, merge_assistant.py, narrate.py    the six offline LLM tools
llm_client.py          shared model-call plumbing (pin+cache, no SDK)
langfuse_sink.py       optional tracing sink

tests/                343 tests
demo_runbook.md        rehearsed six-act demo script, cache-backed for live replay
free-thinking-llm-options.md   design rationale for where each LLM tool plugs in
exercise-q&a.md         design Q&A covering trust, drift, and failure modes

runs/ evals/ proposals/ adjudication/ llm_cache/    gitignored build output
```

## Testing

```bash
python3 -m pytest -q                        # full suite: 343 tests, ~7s
python3 -m pytest tests/test_entity.py -q   # one file
python3 -m pytest -k stability -q           # one pattern
```

Coverage spans the deterministic engine (extraction, normalization,
scoring, entity resolution, field resolution, confidence, trace/replay,
determinism), the eval and label machinery (four-bucket diff, two-pass
label-move guard, adjudication, label import/versioning), and the LLM tool
layer (with the network transport injected out, so the suite never touches
the network or costs an API call).
