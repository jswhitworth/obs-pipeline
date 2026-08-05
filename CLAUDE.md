# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A deterministic, auditable pipeline that resolves raw network-device observations
(ONVIF, SNMP, HTTP banners, mDNS, telnet) into entity records with `vendor`,
`model`, `device_type`, `firmware`, `entity_id` and `confidence`.

`device_fingerprinting_pipeline_design.md` is the specification, and it is
load-bearing rather than historical. Nearly every module docstring and inline
comment cites a section (`§2.3`, `§7.6`, `invariant #5`). **Read the cited section
before changing the code it explains** — most of the non-obvious code exists to
satisfy a specific constraint, and the comment says which one.

`FINDINGS.md` records what the build surfaced: three errors in
`labels/labels-initial.csv`, four internal contradictions in the design document
(and which side the code follows), and gaps left open **on purpose**. Check it
before "fixing" something that looks broken — several apparent defects are
deliberate, including the four missing vendor aliases and the unfixed Stage-1
extraction gaps.

Python 3.11, stdlib + PyYAML only. No package manifest; run scripts from the repo
root.

## Commands

```bash
python3 -m pytest -q                       # full suite (239 tests, ~4s)
python3 -m pytest tests/test_entity.py -q   # one file
python3 -m pytest -k stability -q           # one pattern
python3 -m pytest tests/test_fields.py::test_name -q

python3 run.py                    # pipeline → runs/<run_id>/  (prints run dir)
python3 run.py <obs.csv> <rules_dir> <out_root>

python3 eval.py                   # runs pipeline, scores vs labels → evals/<eval_id>/
python3 eval.py <obs.csv> <rules_dir> <labels.csv> <out_root>

python3 replay.py runs/<run_id>   # trace completeness check; exit 1 on holes
python3 adjudicate.py runs/<run_id>   # blind adjudication packet → adjudication/<run_id>/
python3 label_tools.py            # wide labels-initial.csv → long labels.csv + transitivity check
```

`runs/`, `evals/` and `adjudication/` are gitignored build output. `runs/` is
created on demand; `runs/last_rules_state.json` and `runs/history.jsonl` persist
across runs and are how the version-bump check and trend store work.

## Architecture

### Stage flow (`run.py:run_pipeline`)

```
load_rules ──► load_observations ──► build_claims ──► resolve_entities
                                                            │
                                     observation_fields ◄── resolve_fields
                                                            │
                    entity_confidence + stability ──► write_bundle ──► emit_metrics ──► write_report
```

Each stage is one module in `obs_pipeline/`, and the module map follows the
design's **seven rule categories** — one YAML file per category under `rules/`:

| Module | Rule cat. | Rule file |
|---|---|---|
| `extract.py` | 1 extraction | `extraction.yaml` |
| `normalize.py` | 2 normalization | `normalization.yaml` |
| `claims.py` | 3 claim construction | `claims.yaml` |
| `scoring.py` | 4 scoring (shared) | `scoring.yaml` |
| `entity.py` | 5 clustering + 6 basis precedence | `entity_resolution.yaml` |
| `fields.py` | 7 field resolution + decay | `field_resolution.yaml` |
| `vocab.py` | closed value domains | `canonical_vocab.csv` |

Supporting: `loader.py` (parse + cross-file validation + hashing),
`trace.py` (derivation DAG), `bundle.py` (run bundle writers),
`metrics.py` (label-free metrics), `confidence.py`, `report.py`.

### The invariants that shape the code

These are §4 of the design doc. Violating one is not a style issue — it breaks
the guarantee the whole system exists to provide.

1. **DAG discipline** — entity resolution reads identity claims only, never
   resolved field values. No field→cluster feedback loop.
2. **Canonical keys** — `field` and `link_basis` are fixed vocabularies; `source`
   is metadata *on* the claim, never baked into the key.
3. **Scoring parity** — `scoring.py:score` is the single scoring function, used by
   both claim construction and entity resolution. Coefficients may differ per
   claim type; the formula shape may not. Do not write a second implementation.
4. **Rule-state traceability** — every output binds to the exact rule state via
   `run_id` → `manifest.json`.
5. **Labels never enter the runtime path** — `run.py` and everything it imports
   have no code path that reads `labels/`. This is why `eval.py` and
   `label_tools.py` live *outside* `obs_pipeline/`: structural, not conventional.
6. **Pipeline output never enters hard-stratum label creation** — the mirror of
   #5. In `adjudicate.py`, *selection* may read pipeline output; *presentation*
   must not.
7. **No output value without a derivation** — enforced mechanically by
   `replay.py`, not by convention.

### Trace and replay

`trace.py` uses **trace by construction**: a `Traced` value cannot be created
except through `Tracer.step()`, so a code path that produces a value necessarily
emitted a step. `step_id` is content-addressed, which is what makes two runs over
identical input byte-identical and traces diffable across rule versions — never
replace it with a sequence counter.

`replay.py` reconstructs `claims.csv`, `membership.csv` and `entities.csv` from
`trace.jsonl` **alone**. It must never import the engine — reaching into
`scoring`/`entity`/`fields` would let it recompute a value instead of reading it,
making the guarantee vacuous. `tests/test_replay.py` enforces this.

Absence needs a step too: `no_extraction`, `no_identity_claim`, `vocab_reject` and
`merge_refused` are first-class steps, not silences.

### Run bundle (`runs/<run_id>/`)

`manifest.json`, `metrics.jsonl`, `trace.jsonl`, `claims.csv`, `membership.csv`,
`entities.csv`, `resolutions.csv`, `REPORT.md`.

`REPORT.md` is a **derived render** — regenerable, never hand-edited, and nothing
may depend on parsing it. `resolutions.csv` is a pure join view making no new
decisions, so it emits no trace steps of its own.

Rows stay narrow: every tabular row carries `run_id` and `derivation_step`, which
resolve through `manifest.json` and `trace.jsonl` rather than repeating rule
versions or derivation chains per row.

### Eval harness (`eval.py` → `evals/<eval_id>/`)

Sits outside the pipeline and invokes `run_pipeline` as a black box. It is not a
flag on `run.py`.

- **Four-bucket diff** (`fixed` / `broken` / `stable_correct` / `stable_incorrect`)
  per label, because aggregate metrics hide cases a rule change silently broke.
- **Two-pass refusal**: `evaluate` raises `LabelsMovedError` *before* running or
  writing anything if a baseline is supplied without its `labels_hash`, or if the
  hashes differ. A moved label must never read as a rule regression.
- **`propose_bump`** derives the `rules/VERSION` bump from measured behavior
  (vocabulary narrowing → major, outcomes changed → minor, neutral → patch). It
  is advisory, paired with the load-time hash enforcement below.
- Regressions are **logged, not blocked** — surfaced in `regressions.csv` and in
  `REPORT.md`.

### Rule loading and versioning

`load_rules` validates *across* files before any stage runs — a `link_basis` in
`entity_resolution.yaml` not declared in `claims.yaml`, a `witness_group` with no
`base_weights` entry (which would silently score every claim in it at 0.0), an
escape value outside its own vocabulary column, an observation `source` with no
mapping. All raise `CrossFileError` at init rather than producing an empty
cluster three stages downstream. Add a cross-file reference → add its check.

File hashes are computed on *canonicalized* content, so line-ending churn is not a
rule change. `last_rules_state.json` implements "the hash polices the version":
if the rollup moved but `rules/VERSION` did not, `version_verified` goes false.

## Conventions

- **YAML:** PyYAML `safe_load` only.
- **Determinism:** no reliance on dict ordering, input row order, or wall clock
  anywhere in the pipeline path. Merge order is pinned by
  `entity_resolution.yaml`, not by input order.
- **Never hardcode the field vocabulary.** This is a recurring structural defect
  in this repo's history: three modules once carried private copies of
  `claims.yaml#fields`, so adding a field silently moved published numbers while
  stale consumers ignored the new column. Inside the pipeline read
  `claims.yaml#fields` (`metrics.py:_fields`); when scoring or rendering a
  *written* bundle, read the field list from `entities.csv`'s own header
  (`eval.py:_fields_from_bundle`, `report.py:_field_names`).
- **Three confidence combining operations, deliberately not conflated:** same
  value witnessed twice → max + saturating bonus (`scoring.py`); a propagation
  chain → multiplicative (`fields.py`); different fields into one record →
  harmonic mean (`confidence.py`).
- Claims are **not** vocabulary-constrained; only resolved output is. Rejecting
  out-of-vocab values at claim time would discard the evidence that the
  vocabulary is incomplete.
- Comments here explain *why*, and often cite the section and the failure mode
  they prevent. Match that register rather than adding restating comments.
