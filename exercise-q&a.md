# Exercise Q&A

Scope: the whole repo — the deterministic pipeline, the eval/label
machinery, and the offline LLM tools on this branch.

---

## Q1 — What prevents a wrong output from reaching execution?

*Context: output feeds a downstream system that generates and executes
remediation scripts against live customer OT networks.*

Nothing in this repo executes or blocks execution; the design provides
**(a)** structural properties that make classes of wrong output
impossible to produce, and **(b)** an audited per-row trust contract the
downstream gate is built from — and if the downstream reads only the
value column, nothing here saves you.

### (a) Structural — wrong-output classes that cannot be produced

- **a1. Wrong rule states never run:** `loader.py` cross-validates all
  rule files at init and refuses (`CrossFileError`) for milliseconds of
  cost.
- **a2. Garbage can't reach the value column:** closed vocabularies mean
  `device_type` is `plc` or `unknown`, never `app-webs` — paid for in
  metered coverage loss (`vocab_gap_rate`), not silence.
- **a3. Conflicts resolve to `undecidable`, not a guess:** the live
  Bosch camera reporting firmware 8.10 (ONVIF) vs 8.11 (SNMP) yields
  `undecidable`, costing forgone actions counted in `undecidable_rate`.
- **a4. False merges — the failure that mis-targets a device — are
  structurally disfavored:** OUI can never link, contradictions emit
  `merge_refused`, merge order is pinned, so the characteristic failure
  is a safe duplicate entity.
- **a5. The LLM edge can't touch the runtime:** model output becomes at
  most a *proposed* rule that must survive span-grounded validation, a
  frozen-label regression gate (fixed>0 / broken==0), and a human —
  costing review-cadence latency on new-payload coverage, zero runtime.

### (b) The trust contract — what the downstream gate is built from

- **b1. Audited per-field confidence/provenance/stability:** the gate is
  per-field (*direct provenance, confidence ≥ τ, stability ≥ σ, nothing
  `undecidable`, human tier for PLCs/door controllers*), and calibration
  + stability-quadrant metrics audit that those numbers are honest.
- **b2. Total derivation:** every value replays from `trace.jsonl` to a
  byte-precise payload span and binds to an exact rule state
  (`version_verified`), so the downstream can demand replay-clean
  bundles for ~35 trace steps/observation of storage.
- **b3. The yardstick can't grade itself:** labels are structurally
  isolated from the runtime and from pipeline-influenced creation — how
  the OBS-038 "regression" was exposed as a *label* error — at the cost
  of human adjudication latency.

### What this does *not* prevent

- A confidently-wrong-world extraction (rebadged OEM banner) passes every
  mechanical check; labels correct it on a lag.
- Thresholds calibrated on 74 rows (`same_device` n=7) are statistically
  weak.
- Identity trusts MAC/serial observation; OT networks contain cloned
  MACs.
- The act decision is the downstream's — the guarantee is an honest,
  replayable basis for it.

---

## Q2 — What survives production scale, and what gets torn out?

*Context: 74 rows here; production is several hundred thousand new
observations/month across many customers.*

Grouped as asked: **(a)** survives, **(b)** torn out, **(c)** what
decides which observations see an LLM — the part that keeps (a) and (b)
true, since per-observation inference must never become the cost model.

### (a) Survives

- **a1. The deterministic runtime:** regex + dict lookups per row make
  300K/month single-machine work, and determinism makes partitioning
  trivial.
- **a2. All seven invariants:** none is scale-bound, and derivation +
  rule-state binding get more valuable across customers.
- **a3. Rules as versioned, hash-policed data:** scale-free, and the
  same mechanism versions per-customer overlays.
- **a4. The row trust contract and four-bucket eval discipline.**
- **a5. Trace-by-construction:** storage changes (b1), the design
  doesn't.
- **a6. The LLM sidecar:** offline propose→validate→gate→apply with
  pin-and-cache converts model spend into permanent rules, not per-row
  inference.

### (b) Torn out or rewritten

- **b1. The monolithic batch shape:** one in-memory CSV and repo-local
  state files become partitioned runs, a metadata store, and compressed
  bundles in object storage (~low-GB/month of trace).
- **b2. Cross-run entity identity — the biggest gap:** content-addressed
  ids churn across windows, so stable device identity needs a persistent
  registry that doesn't exist yet.
- **b3. Quadratic corners:** all-pairs edges within shared link values
  need group caps/blocking, and `eval.py`'s all-observation-pairs
  metrics need sampling.
- **b4. Whole-dataset-in-one-prompt tools:** `label_qa` and
  `merge_assistant` become chunked batch jobs over sampled/queued
  subsets; `narrate` becomes on-demand per partition.
- **b5. Exhaustive hand-labeling:** the label machinery survives, but
  active labeling must allocate the label budget per customer.
- **b6. Human-file workflows:** `proposals/` dirs → a review queue,
  public demo traces → private + sampled, `.env` → real secret
  management.

### (c) What decides which observations see an LLM

**None in the serving path — structurally;** offline, selection works
like this:

1. **Template dedup first:** masking serials/MACs/versions collapses
   300K observations to a small set of payload shapes (74 rows ≈ 30),
   making "novel payload format" the unit of LLM cost.
2. **The pipeline's metered queues select templates:**
   `payload_gaps` → `rule_compiler` (ranked by frequency × customers),
   `vocab_reject_frequency` → `alias_miner`, `merge_refused` →
   `merge_assistant` (capped, ranked by blast radius), post-release
   regressions → `rule_copilot`, low-confidence samples →
   `label_qa`/`narrate` under a fixed audit budget.
3. **Cadence and budget:** nightly/weekly Batch API runs whose accepted
   proposals permanently retire their templates, so steady-state spend
   tracks fleet **novelty rate**, not volume — the rebuild demo
   reconstructed the whole ruleset for ~$0.35 and beat hand-written
   recall on three of four fields.
4. **The deliberate non-choice:** no inline/fallback LLM in the hot
   path; if same-day coverage is ever needed, an async once-per-template
   enrichment lane (`source=llm_extract`, low base weight) feeds claims
   while a permanent rule is distilled behind it.
