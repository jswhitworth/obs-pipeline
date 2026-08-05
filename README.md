# Device Fingerprinting Pipeline — Operator Guide

Deterministic, auditable resolution of raw network-device observations
(ONVIF, SNMP, HTTP banners, mDNS, telnet) into entity records with `vendor`,
`model`, `device_type`, `firmware`, `entity_id` and `confidence`.

This document is for people **running** the pipeline. For how it works and
why it's built this way, see `device_fingerprinting_pipeline_design.md`
(the spec) and `CLAUDE.md` (architecture map). For known data issues, see
`FINDINGS.md` — check it before treating any surprising output as a bug.

Python 3.11, stdlib + PyYAML only. No package manifest; run everything from
the repo root.

## Quick start

```bash
python3 -m pytest -q          # 239 tests, ~4s — confirm the checkout is healthy
python3 run.py                # runs the pipeline, prints the run directory
```

That's the whole golden path: rules in `rules/`, input observations in
`obs-data/observations.csv`, output in a fresh `runs/<run_id>/` directory.

## The five entry points

Each is a standalone script, none is a flag on another.

| Script           | Does                                              | Reads labels? |
| ---------------- | ------------------------------------------------- | ------------- |
| `run.py`         | Runs the pipeline                                 | No — never    |
| `replay.py`      | Verifies a run's trace explains its output        | No            |
| `eval.py`        | Runs the pipeline, scores it against ground truth | Yes           |
| `label_tools.py` | Imports/validates the wide label file             | Yes           |
| `adjudicate.py`  | Exports/imports blind adjudication packets        | Yes           |

`run.py` and everything it imports (`obs_pipeline/`) have no code path that
reads `labels/`, by design — the pipeline's output can never
be contaminated by ground truth, and it can never accidentally do better on
labeled data than it would on new data.

### `run.py` — run the pipeline

```bash
python3 run.py                                          # defaults below
python3 run.py <observations.csv> <rules_dir> <out_root>
```

Defaults: `obs-data/observations.csv`, `rules/`, `runs/`. Prints the run
directory it created, e.g. `runs/2026-08-05T03:25:44Z-585855/`, containing:

- `manifest.json` — rule-state fingerprint: per-file hashes, `rules_rollup`,
  `rules_version`, `engine_commit` (git SHA, `-dirty` suffixed if the
  working tree wasn't clean), `input_hash`, `run_id`
- `claims.csv`, `membership.csv`, `entities.csv` — the resolved output
- `resolutions.csv` — a pure join view over the above three, for convenience
- `trace.jsonl` — every derivation step; this is the audit trail
- `metrics.jsonl` — label-free quality metrics (§8), also appended to
  `runs/history.jsonl` for trend tracking
- `REPORT.md` — human-readable render of the above. **Never hand-edit** —
  it says so in its own header. Regenerate with:
  ```bash
  python3 -c "from obs_pipeline.report import write_report; write_report('<run_dir>')"
  ```

Two runs over unchanged input and rules produce byte-identical traces
(determinism is a tested property, not a convention) — re-running to "see if
it changes" will not tell you anything.

### `replay.py` — trace completeness gate

```bash
python3 replay.py runs/<run_id>
```

Reconstructs `claims.csv`, `membership.csv` and `entities.csv` from
`trace.jsonl` **alone** — it never imports the pipeline engine — and diffs
the reconstruction against what actually shipped in the bundle. Exits 1 and
prints the diff on any hole: a value in the output with no derivation step
behind it. This is invariant #7 ("no output value without a derivation")
enforced mechanically, not by review. Run it after any rule or engine change
that touches trace emission; it's cheap and it's the thing that catches a
silently-broken audit trail.

### `eval.py` — score against ground truth

```bash
python3 eval.py                                                     # defaults below
python3 eval.py <observations.csv> <rules_dir> <labels.csv> <out_root>
```

Defaults: `obs-data/observations.csv`, `rules/`, `labels/labels.csv`,
`evals/`. Runs the pipeline (via `run_pipeline`, as a black box) and scores
it, writing `evals/<eval_id>/`:

- `outcomes.json` — one record per (observation, field): `expected` vs
  `actual`, `correct`, `confidence` (entity rollup), `field_confidence`
  (the specific field's own confidence — use this one for calibration
  questions, not the entity rollup), `stability`, `provenance`
  (`direct`/`propagated`), `top1_claim`
- `metrics.jsonl` — the same label-free metrics as a run bundle, plus
  label-aware ones
- `regressions.csv` — four-bucket diff (`fixed`/`broken`/`stable_correct`/
  `stable_incorrect`) against a baseline, if one was supplied
- `eval_manifest.json` — `labels_hash` at eval time, plus baseline
  provenance if compared

It also re-renders the run's own `REPORT.md` with an eval overlay, so
fixed/broken counts land in the report itself and not only in
`regressions.csv`.

**To see the three known label errors** (`FINDINGS.md` §1 — OBS-039,
OBS-040, OBS-041, each a transcription error in `labels-initial.csv` that
contradicts its own payload):

```bash
python3 eval.py
python3 -c "
import json, glob
out = sorted(glob.glob('evals/*/outcomes.json'))[-1]
rows = json.load(open(out))
for r in rows:
    if r['obs_id'] in ('OBS-039', 'OBS-040', 'OBS-041'):
        print(r['obs_id'], r['key'], 'expected=', r['expected'],
              'actual=', r['actual'], 'correct=', r['correct'])
"
```

Then cross-check `expected` against the raw payload in
`obs-data/observations.csv` for that `obs_id` — in all three cases the
pipeline's `actual` matches the payload and the label doesn't. These labels
have deliberately **not** been edited; see `FINDINGS.md` §1 for why
(editing ground truth because the pipeline disagrees is exactly the
contamination invariant #6 exists to prevent) and for the correction's
effect on recall if a human adjudicator eventually fixes them.

**Comparing against a baseline run**, to see what a rules change actually
moved:

```python
from eval import evaluate
evaluate("obs-data/observations.csv", "rules", "labels/labels.csv", "evals",
          baseline_outcomes="evals/<earlier_eval_id>/outcomes.json",
          baseline_labels_hash="<hash from that eval's eval_manifest.json>",
          baseline_run_id="<that eval's run_id>")
```

`evaluate()` refuses to compare (raises `LabelsMovedError`) before writing
anything if the label file moved between baseline and now and you didn't
supply the baseline's hash explicitly — a moved label must never read as a
rule regression.

### `label_tools.py` — import labels

```bash
python3 label_tools.py
```

Converts the wide `labels/labels-initial.csv` (one row per observation,
`vendor`/`model`/`device_type`/`firmware`/`confidence` columns) into the
long-format `labels/labels.csv` that `eval.py` and `adjudicate.py` actually
read, and runs a transitivity check on `same_device` pairs. Prints the
import count and any transitivity problems found. Re-run this after editing
`labels-initial.csv` by hand — `labels.csv` is generated, not maintained
directly.

### `adjudicate.py` — blind adjudication packets

```bash
python3 adjudicate.py runs/<run_id>
```

Exports a packet to `adjudication/<run_id>/` for the observations in the
dual-label/blind-adjudication stratum (`labeler_certainty` in
`medium`/`low` — see `FINDINGS.md` §2 for why certainty, not confidence,
drives selection). The packet carries **evidence only**
(`obs_id`, `obs_hash`, `source`, `raw_payload`, `mac`, `hostname`,
`open_ports`, `site`) — no resolved values, no pipeline confidence, so an
adjudicator is asked "what is this?" rather than "is this right?".

Returned labels are validated with `import_returned_labels(packet_path,
returned_path, observations_path)` (called from Python, no CLI wrapper
yet) — it rejects rows whose `obs_hash` no longer matches the current
observation (the row changed since export) rather than merging silently.
Writing accepted labels back into `labels.csv` is a deliberate open end —
see `FINDINGS.md` §4.

## Everyday workflows

**Changing a rule file and checking the effect:**

```bash
python3 -m pytest -q                 # still green?
python3 eval.py                      # new evals/<id>/
python3 replay.py runs/<run_id>      # trace still explains the output?
```

Then read `regressions.csv` (or the eval overlay in the run's `REPORT.md`)
for what moved. `rules/VERSION` should be bumped to match what
`propose_bump` in `eval.py` would derive from the diff — vocabulary
narrowing is major, outcome changes are minor, a neutral diff is patch.
This is advisory; the load-time check in `loader.py` will report
`version_verified: false` in the manifest if the rules hash moved and the
version didn't.

**Confirming a fresh checkout works end to end:**

```bash
python3 -m pytest -q
python3 run.py
python3 replay.py <the run dir just printed>
python3 label_tools.py
python3 eval.py
python3 adjudicate.py <the run dir>
```

## Things that will surprise you and are not bugs

- `firmware` can resolve to `undecidable` at the entity level (two
  observations of the same device on different firmware versions) while
  the same observation's own `firmware_confidence` and reading survive
  unchanged in `observation_fields` output — firmware is temporal,
  everything else the pipeline resolves is not.
- `device_type` confidence is capped low (~0.25) on essentially every
  observation — `port_signature`'s base weight in `scoring.yaml` is the
  known, documented calibration target (`FINDINGS.md` §3), not a defect.
- Four vendor aliases (`LTS Security`, `Amcrest`, `Wisenet`, `VVTK`) are
  missing from `rules/canonical_vocab.csv` on purpose, so the
  vocabulary-rejection path in `REPORT.md`'s reject queue runs against
  real data instead of being untested dead code.
- `REPORT.md` in any run or eval directory is **generated** — diffing two
  `REPORT.md`s is fine, hand-editing one is not; the next run overwrites it.

Full list, with the data-level detail behind each one, is in
`FINDINGS.md`.
