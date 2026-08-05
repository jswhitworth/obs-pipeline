# Where an LLM could participate

No LLM is used anywhere in this pipeline today, and that is deliberate
(§7.1): an LLM call is neither reproducible nor auditable to a fixed rule,
so it must never enter claim-building as a live source. `run.py` and
everything it imports are stdlib + PyYAML with no network path at all.

This file records where an LLM *could* be introduced without giving that up,
ranked by measured leverage on this dataset. It is a proposal, not a plan —
nothing here is built.

---

## Do

**1. Rule mining from the reject queue (§7.3 Stage 5).**
An LLM reads `metrics.jsonl`'s `vocab_reject_frequency`, the rule that
mis-fired, and the raw payload, then proposes a diff to the relevant YAML.
A human reviews and merges; the pipeline never calls an LLM.

Start with the two known-broken extraction rules:
- `http_server_vendor` over-captures HTTP server software — `app-webs` (8),
  `boa` (2), `apache`, `lighttpd`, `microsoft-httpapi`
- `snmp_vendor_leading` takes only the first token — *Hanwha Techwin* →
  `hanwha` (6)

Together these are **22 of the 29 vocabulary rejects**. The ranked queue is
already emitted in every run bundle (`REPORT.md`, "Vocabulary rejects").

**2. Alias mining.**
The four aliases deliberately left missing (`LTS Security`, `Amcrest`,
`Wisenet`, `VVTK`) map onto existing vocabulary members. Same mechanism as
above, applied to `normalization.yaml`. Trivial case, useful as a first
end-to-end exercise. See `FINDINGS.md` §4 for why they are absent.

**3. Validate every proposal with the harness that already exists.**
An LLM proposal enters as a reviewed YAML change and is judged by machinery
built before anyone considered LLMs:
- `eval.py`'s four-bucket diff (`fixed` / `broken` / `stable_correct` /
  `stable_incorrect`) says per label whether the change helped or quietly
  broke something
- `propose_bump` derives the `rules/VERSION` bump from measured behaviour
- `replay.py` still gates trace completeness

Nothing about determinism, invariant #4, or replay is touched.

---

## Consider

**4. A frozen LLM extraction artifact, scoped to `device_type`.**
§7.1's stated exception: LLM extraction frozen to a checked-in file at
data-prep time — never called live — treated as a low-`base_weight` source
under the same scoring function.

Scope it to `device_type` because that is the one field with genuinely no
evidence source: 15 entities resolve to `unknown` and `vocab_gap_rate` is
0.0, so every one is "no evidence" rather than "rejected evidence".
`port_signature` guesses from open ports at 0.25; a payload reading
`AXIS C1310-E Network Horn Speaker` says plainly it is not a camera.

To stay legitimate it needs:
- the artifact hashed into `manifest.json` (its own hash, or inside the
  rules rollup), so `run_id` still binds output to exact input state
- a `witness_group` and a `base_weight` in `scoring.yaml`, below
  `port_signature`
- extraction steps citing `llm_extraction.csv#<row_hash>` as `rule_id`, so
  invariant #7 holds — the value has a derivation, pointing at a frozen row

Determinism survives because the artifact is data, not a call.

**5. An LLM as one slot in dual-labeling (§7.2.3).**
Add an `llm_inference` tier to `BASIS_PRECEDENCE`, **below**
`payload_inference`. Then it can never overrule a human label, it can
populate a slot no human has labelled, and its *disagreement* with a human
label routes scarce human attention to contested cases.

The product is the disagreement signal, not the answer.
`resolve_by_basis_precedence` already handles the tiering with no new logic.

---

## Don't

**6. Any live call in `run.py`'s path.**
Kills determinism (§10), byte-identical traces, and invariant #4's binding
of every output to an exact rule state.

**7. Scoring or entity resolution.**
Invariant #3 is one shared scoring function; merge order is pinned in
`entity_resolution.yaml` precisely so outcomes do not depend on evaluation
order. An LLM deciding merges makes `replay.py` vacuous.

**8. Confidence.**
It must decompose from the trace (`base_max + bonus − penalty`, components
recorded). A number an LLM produced cannot be decomposed, and the
calibration work in `FINDINGS.md` §3 stops meaning anything.

**9. Unblinded LLM adjudication.**
Tempting — §7.7's stratum is 19 observations of human work — and wrong. An
LLM reading `raw_payload` performs the same text-pattern inference the
regexes do, over overlapping training text. Its agreement with the pipeline
is correlation, not corroboration. Ground truth built that way makes
Stages 1–4 measure self-consistency, which §7.7 names as "a silent,
self-reinforcing failure, worst precisely where the pipeline is confidently
wrong."

Item 5 is the narrow, defensible version of this idea; item 9 is what it
must not become.

---

## Where to start

**Item 1.** Lowest risk, highest measured leverage, and one `eval.py` run
tells you whether it worked.
