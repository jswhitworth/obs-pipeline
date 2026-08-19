# Free-thinking: where LLMs add value in this pipeline

A fresh look at the pipeline as it actually is — raw device observations (ONVIF,
SNMP sysDescr, HTTP banners, mDNS TXT, telnet) resolved into entity records with
vendor / model / device_type / firmware / confidence — and where a language model
genuinely earns its keep. Ideas are grouped by how the LLM plugs in, because that
matters more than which stage it touches.

---

## 1. The single biggest fit: extraction from raw payloads

`raw_payload` is exactly the kind of input LLMs are best at and regex rules are
worst at: semi-structured vendor prose that never agrees on a format.

```
Manufacturer=AXIS; Model=P3245-LVE; FirmwareVersion=10.12.114
HTTP/1.1 200 OK | Server: Axis/10.12.114 (Linux) | Digest realm="AXIS_ACCC8E4F21A9"
Wisenet XRN-2010 Network Video Recorder, S/N ZN9K8H2M4001, FW 2.11.04
```

Hand-written extraction rules will always trail the long tail of vendors,
firmware string formats, and banner quirks. An LLM with a tight schema prompt
("return vendor, model, firmware, serial, device_type or null, with a source span
for each") will extract correctly from payloads no rule has ever seen — including
vendors that appear once in the dataset.

Three deployment shapes, in increasing order of conservatism:

- **a. Inline extractor.** LLM is a first-class extraction source alongside the
  rule-based one; its claims carry `source=llm_extract` and their own scoring
  coefficients (probably lower base weight than a structured ONVIF field, higher
  than a fuzzy hostname token). Cheapest to build, highest ongoing inference cost.
- **b. Fallback extractor.** Rules run first; the LLM only sees payloads that
  produced *no* extraction (the `no_extraction` cases). This targets spend exactly
  at the current blind spots and leaves the hot path untouched.
- **c. Rule compiler.** The LLM never runs at pipeline time. Offline, it reads
  the corpus of unmatched payloads and *writes extraction rules* (patterns +
  tests), which a human reviews and merges. The runtime stays cheap and fully
  reproducible; the LLM's knowledge gets distilled into auditable artifacts.
  This is the highest-leverage option: one LLM pass improves every future run
  for free.

(b) and (c) compose well: fallback in production, and periodically compile the
fallback's accumulated hits into permanent rules.

## 2. Normalization and alias discovery

"AXIS" vs "Axis Communications", "Wisenet" vs "Hanwha Techwin" vs "Hanwha
Vision", model strings with and without suffixes ("P3245-LVE" vs "AXIS P3245-LVE
Network Camera"). LLMs carry a lot of real-world knowledge about the device
market — vendor acquisitions, rebrands, OEM relationships — that no normalization
table in this repo will ever fully enumerate.

Concrete uses:

- **Alias mining (offline):** feed the distinct raw vendor/model strings observed
  across runs; the LLM proposes normalization mappings with a rationale
  ("Wisenet is Hanwha's camera brand, formerly Samsung Techwin"). Output is a
  proposed diff to `normalization.yaml`, human-reviewed.
- **Vocabulary expansion:** values rejected as out-of-vocab are a queue the LLM
  can triage — "these 14 rejected model strings are 3 real new models plus 11
  variants of things already in the vocab, here's the mapping." Turns a silent
  loss bucket into a curated growth path for `canonical_vocab.csv`.
- **device_type inference:** the LLM knows an XRN-2010 is an NVR and a P3245 is
  a camera without being told. Useful as a fallback claim source or as a
  consistency checker over resolved output.

## 3. Entity resolution: the ambiguous middle

Deterministic clustering on MAC/serial/hostname handles the easy 90%. The value
of an LLM is in the residue:

- **Merge adjudication assistant:** for borderline pairs (shared hostname token
  but different MACs; serial match but conflicting models), an LLM given both
  claim sets can reason like the human who would otherwise review it — "same
  serial format, model strings differ only by region suffix, likely one device"
  vs "MAC OUIs belong to different vendors, likely a rebadged NVR fronting a
  camera." Run it on the `merge_refused` / low-margin cases and queue its verdicts
  with rationales for a human to accept in bulk.
- **Heuristic discovery:** ask the LLM to study clusters that humans later
  corrected and propose new link heuristics ("Axis embeds the MAC in the digest
  realm — `AXIS_ACCC8E4F21A9` — that's a linkable identity signal you're not
  using"). That specific one is visible in the sample data above and is exactly
  the kind of cross-field signal rules-by-hand tend to miss.

## 4. Label QA — yes, LLMs are good at catching human labeling errors

The labeled data is small, hand-made, and has known error modes (typos,
inconsistent vendor spellings, transitivity slips in same-device judgements,
labels that contradict the raw payload they annotate). An LLM is a strong
*auditor* here:

- **Payload–label contradiction sweep:** for every label, show the LLM the
  underlying observation(s) and the label; flag rows where the payload plainly
  says something else (label says `ip_camera`, sysDescr says "Network Video
  Recorder"). This catches exactly the class of error humans make when labeling
  hundreds of rows: right column, wrong row; copy-paste drift; stale entity_id.
- **Internal consistency sweep:** same entity_id with two vendors, firmware
  labeled on an observation whose payload carries no firmware, confidence grades
  that don't match evidence strength.
- **Correction proposals, not corrections:** the right output is a
  `label_suspects.csv` — row, suspected error, proposed fix, evidence quote,
  LLM confidence — that a human accepts or rejects through the existing
  adjudication path. Two reasons this "propose, don't overwrite" shape is my own
  hard recommendation and not timidity:
  1. The labels are the yardstick the pipeline is scored against. If a model
     rewrites the yardstick and a model-informed pipeline is measured by it,
     correlated errors become invisible — scores go up whether or not accuracy
     did. Keeping a human accept step preserves the labels' independence, which
     is the only thing that makes eval numbers mean anything.
  2. It's nearly free: the LLM does 99% of the work (finding the needles), the
     human does the 1% that keeps the ground truth trustworthy. In practice this
     is *faster* than letting the LLM write directly, because you never have to
     forensically un-tangle a bad batch later.
- **Active labeling:** invert it — have the LLM rank *unlabeled* observations by
  expected information value ("this cluster is the only Bosch in the set and the
  pipeline is least confident about it") so human labeling minutes go where they
  buy the most eval power.

## 5. Confidence, anomaly triage, and reporting

- **Low-confidence explanation:** for entities that resolve with weak confidence,
  an LLM reading the claims and trace can produce a one-line human diagnosis
  ("two sources agree on vendor, firmware comes only from a stale telnet banner")
  — far more actionable in REPORT.md than a bare 0.42.
- **Run-diff narration:** after a rule change, the eval diff (fixed / broken /
  stable buckets) is mechanical; the LLM's job is the story — "the new Hanwha
  alias fixed 6 NVRs but broke 2 cameras because 'Wisenet' also appears in a
  reseller's HTTP banner." That's the paragraph a reviewer actually wants.
- **Anomaly spotting:** an LLM skimming resolved entities catches things metrics
  don't encode: a camera model on an NVR port profile, firmware versions that
  don't exist for that model line, a MAC OUI inconsistent with the resolved
  vendor. Each is a cheap nightly report.

## 6. Rule authoring copilot

The eval harness already closes the loop mechanically (run → score → regressions
surfaced). The missing piece is the expensive human step in the middle: reading a
regression and writing the rule diff. An LLM is well-suited to sit exactly there:

- Input: a regression row + the observations behind it + the current rule files.
- Output: a candidate rule diff + a plain-English rationale.
- Validation: the *existing* eval harness — run it, and only surface diffs that
  fix the target without breaking the stable-correct bucket.

This turns the eval harness into a fitness function for LLM-proposed patches.
Fully automatic proposal, mechanically gated acceptance, human merge. Of all the
ideas here, this one compounds the most: every accepted patch is permanent.

## 7. Keeping any of this trustworthy (my own engineering view)

Not restrictions inherited from anywhere — just what I'd insist on in any
fingerprinting system before putting a model in it:

- **Pin and cache.** Key every LLM call on (model id, prompt hash, input hash)
  and cache the response. Reruns over identical input stay identical; a model
  upgrade is an explicit, diffable event like a rule bump, not silent drift.
- **Provenance on every model-derived value.** `source=llm_*` on claims,
  `origin=llm_proposed` on labels/rules. You want the ability, six months in, to
  ask "how much of what we believe came from the model, and is it holding up?"
- **Prefer distillation to inference.** Wherever the LLM's output can be
  compiled into a static artifact (rules, aliases, vocab entries), do that.
  It's cheaper, faster, auditable, and the LLM's judgement gets reviewed once
  instead of trusted forever.
- **Keep the yardstick independent.** Model output can *nominate* label
  corrections and new labels; a human keystroke should stand between nomination
  and ground truth (see §4 for why this is load-bearing, not ceremonial).

## Priority order, if I were sequencing this

1. **§1c Rule compiler** on the `no_extraction` backlog — biggest accuracy gain
   per dollar, zero runtime footprint.
   **✅ Implemented** — `rule_compiler.py` (`propose` per model, `compare`
   across models). Offline only; pin-and-cache keyed on (model, prompt);
   `origin: llm_proposed` provenance on every rule; proposals validated
   locally, then scored per model by the existing eval harness (shared
   baseline, four-bucket diff) so metrics are comparable between models.
   Human review + manual merge into `rules/extraction.yaml` remains the
   acceptance step. Tests: `tests/test_rule_compiler.py`.
2. **§4 Label QA sweep** — the dataset is small enough to audit in one batch and
   every eval number downstream gets more honest.
   **✅ Implemented** — `label_qa.py`. One-batch payload-vs-label +
   consistency sweep → `label_suspects.csv` (row, error, proposed fix,
   evidence quote, confidence). Propose-don't-overwrite: acceptance flows
   through the existing adjudication path; the tool reads no pipeline
   output, so pipeline opinion cannot launder into ground truth. First live
   sweep found 4 genuine label errors (OBS-038/039/040/041), all verified
   against payloads.
3. **§6 Rule copilot** — the harness to gate it already exists.
   **✅ Implemented** — `rule_copilot.py`. Reads an eval dir's incorrect
   outcomes, proposes extraction-rule + alias patches, and gates them
   through `eval.evaluate` against that eval's own baseline: surfaced only
   when fixed>0 and broken==0. First live run: 37/54 misses fixed, 1
   "broken" — which turned out to be label error OBS-038 (flagged
   independently by the §4 sweep), so the gate correctly held the diff
   until the yardstick is fixed.
4. **§2 Alias mining** — one-time large gain, then occasional maintenance.
   **✅ Implemented** — `alias_miner.py`. Backlog = out-of-vocab claim
   values from a written run; proposes `normalization.yaml` alias entries
   (validated: target must already be a vocab member) and advisory
   `new_vocab` nominations (never auto-merged). Proposal dirs are
   `rule_compiler.py compare`-compatible for cross-model metrics. Live:
   both models recovered the deliberately-missing aliases (wisenet,
   amcrest, …); vendor top1_claim_accuracy 0.643 → 0.829 with 0 broken.
5. **§3 Merge adjudication assistant** — highest judgement content, so do it
   once the cheaper wins have built confidence in the harness/QA loop.
   **✅ Implemented** — `merge_assistant.py`. Cases = `merge_refused` trace
   steps (below_threshold pairs, cross_basis conflicts); the LLM sees both
   claim sets and queues advisory verdicts in `merge_verdicts.csv` for
   human bulk-accept. Never writes labels (invariant #6: selection may read
   pipeline output; label creation stays in the adjudication path). The
   current dataset has zero refusals, so the live run correctly no-ops
   without an API call.
6. **§5 Reporting/narration** — easy and pleasant, but it improves the reading
   of results rather than the results, so it goes last.
   **✅ Implemented** — `narrate.py`. Offline render of a written bundle
   (+ optional eval overlay) into `NARRATIVE.md` beside REPORT.md: run
   summary, one-line diagnoses per low-confidence entity, anomaly list.
   Advisory and regenerable; the runtime report writer stays model-free.

All six items share `llm_client.py` (§7's pin-and-cache keyed on the exact
request body, `origin: llm_proposed` provenance, injectable transport so
tests never touch the network) and take `--model` for cross-model
comparison (default `claude-sonnet-5`; haiku for cheap comparison runs,
opus reserved for deliberate runs). Tests: `tests/test_rule_compiler.py`,
`tests/test_llm_tools.py`.

**Rebuild demo**: `rebuild_demo.py` ablates the hand-written extraction
knowledge (regex rules + aliases; the structural skeleton stays) and lets
rule_compiler (payload_gaps backlog) + alias_miner build it back round by
round, applying accepted fragments via the sanctioned `apply` commands and
charting recall/coverage per round. First live run (claude-sonnet-5, 3
rounds, ~$0.35): 50 rules + 16 aliases rebuilt, 107 label outcomes fixed,
1 "broken" (the OBS-038 label error), and the rebuilt ruleset BEAT the
hand-written baseline on firmware recall (0.96 vs 0.81), model (0.66 vs
0.65) and vendor (0.93 vs 0.92).

**Observability**: `langfuse_sink.py` emits one public Langfuse trace per
tool run that called a model (generation + validation spans + eval-derived
scores such as fixed/broken/surfaced), keyed to the same provenance
(run_id, cache_key, labels_hash) and grouped into sessions by run_id.
Enabled automatically when LANGFUSE_* keys are present in .env; telemetry
failures never fail a tool, and the pipeline itself is never instrumented
-- trace.jsonl remains the authoritative trace system for the
deterministic path.
