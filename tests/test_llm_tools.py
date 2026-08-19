"""Items #2-#6 of free-thinking-llm-options.md: alias mining (§2), label QA
(§4), rule copilot (§6), merge assistant (§3), narration (§5).

Same discipline as test_rule_compiler.py: every test injects a fake
transport (the network seam is the tools' only non-deterministic edge), and
every output root / cache dir is a tmp path -- the CWD-relative defaults
(proposals/, llm_cache/) are guarded by conftest.py.
"""
import csv
import json
from pathlib import Path

import pytest
import yaml

import alias_miner
import label_qa
import merge_assistant
import narrate as narrate_mod
import rule_copilot
from eval import evaluate
from label_tools import labels_hash, load_labels
from obs_pipeline.loader import load_rules
from run import run_pipeline

OBS = "obs-data/observations.csv"
LABELS = "labels/labels.csv"


def _api_response(parsed, stop_reason="end_turn"):
    return {
        "stop_reason": stop_reason,
        "content": [{"type": "text", "text": json.dumps(parsed)}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _transport(parsed, calls=None):
    def transport(body):
        if calls is not None:
            calls.append(body)
        return _api_response(parsed)
    return transport


@pytest.fixture(scope="module")
def base_run(tmp_path_factory):
    return run_pipeline(OBS, "rules", tmp_path_factory.mktemp("runs"))


@pytest.fixture(scope="module")
def base_eval(tmp_path_factory):
    return evaluate(OBS, "rules", LABELS, tmp_path_factory.mktemp("evals"),
                    runs_root=tmp_path_factory.mktemp("eruns"))


# --------------------------------------------------------------------------
# §2 alias mining
# --------------------------------------------------------------------------

WISENET_ALIAS = {"block": "vendor_alias", "surface": "wisenet",
                 "target": "Hanwha Vision",
                 "rationale": "Wisenet is Hanwha's camera brand"}


def test_alias_backlog_is_the_out_of_vocab_claim_values(base_run):
    backlog = alias_miner.collect_backlog(base_run)
    wisenet = next(i for i in backlog if i["value"] == "wisenet")
    assert wisenet["key"] == "vendor"
    assert all(i["count"] >= 1 and i["obs_ids"] for i in backlog)


def test_alias_validation_rejects_unfounded_entries(base_run, tmp_path):
    bad = [
        # target not an existing vocab member -> belongs in new_vocab
        {**WISENET_ALIAS, "target": "Wisenet Inc."},
        # surface not in lookup-key form
        {**WISENET_ALIAS, "surface": "Wisenet"},
        # surface already mapped
        {**WISENET_ALIAS, "surface": "axis",
         "target": "Axis Communications"},
        # surface never observed in this run's backlog
        {**WISENET_ALIAS, "surface": "acme"},
    ]
    out = alias_miner.propose(
        base_run, "model-a", out_root=tmp_path / "p",
        cache_dir=tmp_path / "cache",
        transport=_transport({"aliases": bad + [WISENET_ALIAS],
                              "new_vocab": [{"column": "vendor",
                                             "value": "Acme Cameras",
                                             "rationale": "seen once"}]}))
    proposal = json.loads((out / "proposal.json").read_text())
    assert list(proposal["accepted"]) == ["vendor_alias:wisenet"]
    assert len(proposal["rejected"]) == len(bad)
    # new_vocab is advisory only: recorded, but the candidate vocabulary
    # file is byte-identical to the live one.
    assert proposal["new_vocab"][0]["value"] == "Acme Cameras"
    assert (out / "rules" / "canonical_vocab.csv").read_bytes() == \
        Path("rules/canonical_vocab.csv").read_bytes()
    # candidate normalization carries the accepted alias and still loads
    merged = yaml.safe_load((out / "rules" / "normalization.yaml").read_text())
    assert merged["vendor_alias"]["map"]["wisenet"] == "Hanwha Vision"
    load_rules(out / "rules")


def test_alias_fragment_is_normalization_shaped(base_run, tmp_path):
    out = alias_miner.propose(
        base_run, "model-a", out_root=tmp_path / "p",
        cache_dir=tmp_path / "cache",
        transport=_transport({"aliases": [WISENET_ALIAS], "new_vocab": []}))
    fragment = yaml.safe_load((out / "proposed_normalization.yaml").read_text())
    assert fragment == {"vendor_alias": {"map": {"wisenet": "Hanwha Vision"}}}


# --------------------------------------------------------------------------
# §4 label QA
# --------------------------------------------------------------------------

def _suspect(**over):
    base = {"obs_id": "OBS-044", "key": "vendor", "current_value": "Unknown",
            "proposed_value": "Axis Communications",
            "suspected_error": "truncated banner still names the vendor",
            "evidence": "Server: Ax", "confidence": "low"}
    return {**base, **over}


def test_label_qa_writes_suspects_and_never_touches_labels(tmp_path):
    before = Path(LABELS).read_bytes()
    bad = [
        _suspect(obs_id="OBS-999"),                       # no such row
        _suspect(current_value="Axis Communications"),    # mis-quoted value
        _suspect(proposed_value="Unknown"),               # no-op proposal
    ]
    out = label_qa.sweep(
        LABELS, OBS, "model-a", out_root=tmp_path / "p",
        cache_dir=tmp_path / "cache",
        transport=_transport({"suspects": bad + [_suspect()]}))
    assert Path(LABELS).read_bytes() == before
    rows = list(csv.DictReader(
        (out / "label_suspects.csv").open(encoding="utf-8")))
    assert [(r["obs_id"], r["key"]) for r in rows] == [("OBS-044", "vendor")]
    assert rows[0]["origin"] == "llm_proposed"
    assert rows[0]["model"] == "model-a"
    proposal = json.loads((out / "proposal.json").read_text())
    assert len(proposal["rejected"]) == len(bad)
    assert proposal["labels_hash"] == labels_hash(LABELS)


def test_label_qa_validates_same_device_pairs_against_labels():
    labels = load_labels(LABELS)
    ok = {"obs_id": "OBS-004", "key": "same_device",
          "current_value": "OBS-005", "proposed_value": "(remove)",
          "suspected_error": "x", "evidence": "y", "confidence": "low"}
    assert label_qa.validate_suspect(ok, labels) == []
    assert label_qa.validate_suspect(
        {**ok, "current_value": "OBS-050"}, labels)


# --------------------------------------------------------------------------
# §6 rule copilot
# --------------------------------------------------------------------------

def test_copilot_targets_are_the_incorrect_outcomes(base_eval):
    targets = rule_copilot.collect_targets(base_eval)
    assert targets, "expected the known stable_incorrect cases"
    assert all(t["correct"] is False for t in targets)
    # OBS-004's device_type miss (sysDescr says "Network Video Recorder",
    # the port signature says camera) must be in the target set.
    assert ("OBS-004", "device_type") in {(t["obs_id"], t["key"])
                                          for t in targets}


NVR_RULE = {
    "name": "snmp_devicetype_phrase",
    "source": "snmp_sysdescr",
    "pattern": r"(?P<v>Network Video Recorder)",
    "target_kind": "field",
    "target": "device_type",
    "rationale": "sysDescr names the device class outright; the port "
                 "signature mislabels NVRs as cameras",
    "tests": [{"payload": "Wisenet XRN-2010 Network Video Recorder, "
                          "S/N ZN9K8H2M4001, FW 2.11.04",
               "expect": "Network Video Recorder"}],
}


def test_copilot_gates_through_the_eval_harness(base_eval, tmp_path):
    """A realistic patch for the OBS-004/005 device_type miss; the gate must
    score it via eval.evaluate and surface it only on fixed>0 / broken==0."""
    out = rule_copilot.propose(
        base_eval, "model-a", observations_path=OBS,
        out_root=tmp_path / "p", cache_dir=tmp_path / "cache",
        transport=_transport({"extraction_rules": [NVR_RULE],
                              "aliases": [],
                              "notes": "sysDescr device class phrase"}))
    proposal = json.loads((out / "proposal.json").read_text())
    assert list(proposal["accepted"]) == ["rule:snmp_devicetype_phrase"]
    fb = proposal["four_bucket"]
    assert set(fb) == {"fixed", "broken", "stable_correct",
                       "stable_incorrect"}
    assert proposal["surfaced"] == (fb["fixed"] > 0 and fb["broken"] == 0)
    assert (out / "gate").exists()


def test_copilot_apply_refuses_an_ungated_diff(base_eval, tmp_path):
    """apply exists only downstream of the fitness function: a diff the
    gate did not surface (fixed>0, broken==0) must not be appliable."""
    out = rule_copilot.propose(
        base_eval, "model-a", observations_path=OBS,
        out_root=tmp_path / "p", cache_dir=tmp_path / "cache",
        transport=_transport({"extraction_rules": [NVR_RULE], "aliases": [],
                              "notes": ""}))
    proposal = json.loads((out / "proposal.json").read_text())
    if not proposal["surfaced"]:
        with pytest.raises(RuntimeError, match="not surfaced"):
            rule_copilot.apply_patches(out, tmp_path / "rules")
    else:  # if the gate did surface it, apply must work on a rules copy
        import shutil
        work = tmp_path / "rules"
        work.mkdir()
        for name in (*rc.RULE_FILES, rc.VOCAB_FILE, "VERSION"):
            shutil.copy(Path("rules") / name, work / name)
        assert rule_copilot.apply_patches(out, work) >= 1
        load_rules(work)


def test_copilot_with_nothing_accepted_never_runs_the_gate(base_eval,
                                                           tmp_path):
    out = rule_copilot.propose(
        base_eval, "model-a", observations_path=OBS,
        out_root=tmp_path / "p", cache_dir=tmp_path / "cache",
        transport=_transport({"extraction_rules": [], "aliases": [],
                              "notes": "no safe patch"}))
    proposal = json.loads((out / "proposal.json").read_text())
    assert proposal["surfaced"] is False
    assert proposal["four_bucket"] is None
    assert not (out / "gate").exists()


# --------------------------------------------------------------------------
# §3 merge assistant
# --------------------------------------------------------------------------

def _synthetic_refusal_run(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text(json.dumps(
        {"run_id": "synthetic", "rules_rollup": "sha256:0"}))
    steps = [
        {"op": "merge_refused", "reason": "below_threshold",
         "inputs": ["obs:OBS-A", "obs:OBS-B"],
         "detail": {"basis": "hostname_token", "link_weight": 0.2,
                    "threshold": 0.55}},
        {"op": "extract", "inputs": ["obs:OBS-A"], "output": "x"},
    ]
    (run_dir / "trace.jsonl").write_text(
        "\n".join(json.dumps(s) for s in steps) + "\n")
    with open(run_dir / "claims.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["run_id", "derivation_step", "obs_id", "kind", "key",
                    "value", "weight", "witness_groups", "sources",
                    "in_vocab"])
        w.writerow(["synthetic", "sha256:1", "OBS-A", "field", "vendor",
                    "Axis Communications", "0.9", "g", "s", "True"])
        w.writerow(["synthetic", "sha256:2", "OBS-B", "field", "vendor",
                    "Hikvision", "0.9", "g", "s", "True"])
    obs = tmp_path / "obs.csv"
    with open(obs, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["obs_id", "source", "raw_payload", "mac", "hostname",
                    "open_ports", "ip_address", "site"])
        w.writerow(["OBS-A", "http_banner", "Server: Axis/1.0", "", "cam-1",
                    "80", "10.0.0.1", "X"])
        w.writerow(["OBS-B", "http_banner", "Server: Hikvision", "", "cam-1",
                    "80", "10.0.0.2", "X"])
    return run_dir, obs


def test_merge_assistant_collects_refusals_and_validates_verdicts(tmp_path):
    run_dir, obs = _synthetic_refusal_run(tmp_path)
    cases = merge_assistant.collect_cases(run_dir)
    assert [c["case_id"] for c in cases] == ["OBS-A|OBS-B"]
    verdicts = [
        {"case_id": "OBS-A|OBS-B", "verdict": "different_device",
         "rationale": "different vendors on both claim sets",
         "confidence": "high"},
        {"case_id": "OBS-A|OBS-B", "verdict": "same_device",
         "rationale": "dup", "confidence": "low"},        # duplicate
        {"case_id": "OBS-X|OBS-Y", "verdict": "unclear",
         "rationale": "n/a", "confidence": "low"},        # unknown case
    ]
    out = merge_assistant.assist(
        run_dir, "model-a", observations_path=obs,
        out_root=tmp_path / "p", cache_dir=tmp_path / "cache",
        transport=_transport({"verdicts": verdicts}))
    proposal = json.loads((out / "proposal.json").read_text())
    assert len(proposal["accepted"]) == 1
    assert len(proposal["rejected"]) == 2
    rows = list(csv.DictReader(
        (out / "merge_verdicts.csv").open(encoding="utf-8")))
    assert rows[0]["case_id"] == "OBS-A|OBS-B"
    assert rows[0]["verdict"] == "different_device"
    assert rows[0]["origin"] == "llm_proposed"


def test_merge_assistant_with_no_refusals_makes_no_call(base_run, tmp_path):
    def explode(body):
        raise AssertionError("no merge_refused cases -> no API call")

    out = merge_assistant.assist(
        base_run, "model-a", observations_path=OBS,
        out_root=tmp_path / "p", cache_dir=tmp_path / "cache",
        transport=explode)
    proposal = json.loads((out / "proposal.json").read_text())
    assert proposal["cases"] == [] and proposal["cache_key"] is None


# --------------------------------------------------------------------------
# §5 narration
# --------------------------------------------------------------------------

def test_narrate_writes_an_advisory_derived_render(base_run, base_eval,
                                                   tmp_path):
    entities = list(csv.DictReader(
        (base_run / "entities.csv").open(encoding="utf-8")))
    real_id = entities[0]["entity_id"]
    parsed = {
        "run_summary": "Most entities resolved cleanly.",
        "entity_notes": [
            {"entity_id": real_id, "note": "firmware from a single banner"},
            {"entity_id": "E-nonexistent", "note": "must be dropped"},
        ],
        "anomalies": [{"subject": real_id,
                       "note": "camera model on an NVR port profile"}],
    }
    out = narrate_mod.narrate(
        base_run, "model-a", eval_dir=base_eval,
        out_path=tmp_path / "NARRATIVE.md", cache_dir=tmp_path / "cache",
        transport=_transport(parsed))
    text = out.read_text()
    assert "Most entities resolved cleanly." in text
    assert real_id in text
    assert "E-nonexistent" not in text        # unknown entity ids filtered
    assert "advisory" in text and "model-a" in text
    manifest = json.loads((base_run / "manifest.json").read_text())
    assert manifest["run_id"] in text


def test_narrate_defaults_next_to_report(base_run, tmp_path):
    parsed = {"run_summary": "s", "entity_notes": [], "anomalies": []}
    out = narrate_mod.narrate(base_run, "model-a",
                              cache_dir=tmp_path / "cache",
                              transport=_transport(parsed))
    assert out == base_run / "NARRATIVE.md"
    assert (base_run / "REPORT.md").exists()  # sits beside, never replaces


# --------------------------------------------------------------------------
# Langfuse sink (telemetry for the LLM edge; injectable, never load-bearing)
# --------------------------------------------------------------------------

import langfuse_sink


def _sink_transport(captured):
    def transport(path, payload):
        captured.append((path, payload))
        if payload is None:  # GET /projects for URL construction
            return {"data": [{"id": "proj-123"}]}
        return {"successes": [], "errors": []}
    return transport


def test_langfuse_sink_builds_a_public_trace_batch():
    captured = []
    url = langfuse_sink.emit_tool_trace(
        tool="alias_miner", model="claude-sonnet-5", prompt="P",
        output_text="O", usage={"input_tokens": 10, "output_tokens": 5},
        cached=False, cache_key="sha256:abc", elapsed_ms=123.4,
        metadata={"run_id": "r1"}, spans=[("validate", {"accepted": 2})],
        scores=[("accepted_aliases", 2)], session_id="r1",
        transport=_sink_transport(captured))
    (ingest_path, ingest), (projects_path, none) = captured
    assert ingest_path.endswith("/ingestion") and none is None
    kinds = [e["type"] for e in ingest["batch"]]
    assert kinds == ["trace-create", "generation-create", "span-create",
                     "score-create"]
    trace, gen, span, score = [e["body"] for e in ingest["batch"]]
    assert trace["public"] is True
    assert trace["sessionId"] == "r1"
    assert trace["metadata"]["origin"] == "llm_proposed"
    assert gen["usage"] == {"input": 10, "output": 5, "unit": "TOKENS"}
    assert gen["traceId"] == trace["id"]
    assert score["value"] == 2.0
    assert url is not None and trace["id"] in url and "proj-123" in url


def test_langfuse_sink_failure_never_breaks_the_tool():
    def explode(path, payload):
        raise OSError("network down")
    url = langfuse_sink.emit_tool_trace(
        tool="t", model="m", prompt="p", output_text="o", usage=None,
        cached=True, cache_key="sha256:x", elapsed_ms=0.0,
        transport=explode)
    assert url is None


def test_sink_threads_through_a_tool_and_lands_in_provenance(base_run,
                                                             tmp_path):
    calls = []

    def fake_sink(**kwargs):
        calls.append(kwargs)
        return "https://example/trace/1"

    out = alias_miner.propose(
        base_run, "model-a", out_root=tmp_path / "p",
        cache_dir=tmp_path / "cache",
        transport=_transport({"aliases": [WISENET_ALIAS], "new_vocab": []}),
        sink=fake_sink)
    assert len(calls) == 1
    assert calls[0]["tool"] == "alias_miner"
    assert calls[0]["model"] == "model-a"
    assert ("accepted_aliases", 1) in calls[0]["scores"]
    proposal = json.loads((out / "proposal.json").read_text())
    assert proposal["trace_url"] == "https://example/trace/1"


def test_sink_is_not_called_when_no_model_ran(base_run, tmp_path):
    def fail_sink(**kwargs):
        raise AssertionError("no LLM call -> no trace")

    merge_assistant.assist(
        base_run, "model-a", observations_path=OBS,
        out_root=tmp_path / "p", cache_dir=tmp_path / "cache",
        transport=lambda body: (_ for _ in ()).throw(AssertionError()),
        sink=fail_sink)


# --------------------------------------------------------------------------
# rules-rebuild demo (ablate -> propose -> apply -> eval)
# --------------------------------------------------------------------------

import rebuild_demo
import rule_compiler as rc


def test_ablate_strips_learned_knowledge_but_stays_loadable(tmp_path):
    work = rebuild_demo.ablate("rules", tmp_path / "ablated")
    extraction = yaml.safe_load((work / "extraction.yaml").read_text())
    normalization = yaml.safe_load((work / "normalization.yaml").read_text())
    assert extraction["rules"] == {}
    assert extraction["structured"]          # skeleton survives
    assert normalization["vendor_alias"]["map"] == {}
    load_rules(work)


def test_payload_gaps_backlog_sees_what_no_extraction_hides(tmp_path):
    """With regex rules ablated, the mac-column lift still fires, so
    `no_extraction` stays tiny while the payload gap is nearly total."""
    work = rebuild_demo.ablate("rules", tmp_path / "ablated")
    run_dir = run_pipeline(OBS, work, tmp_path / "runs")
    narrow = rc.collect_backlog(run_dir, OBS, "no_extraction")
    wide = rc.collect_backlog(run_dir, OBS, "payload_gaps")
    assert len(wide) > 40
    assert len(narrow) < len(wide)
    assert "OBS-001" in {c["obs_id"] for c in wide}


REBUILT_RULES = [
    {"name": "onvif_manufacturer_rebuilt",
     "source": "onvif_device_info",
     "pattern": r"Manufacturer=(?P<v>[^;]+)",
     "target_kind": "field", "target": "vendor",
     "rationale": "ONVIF key/value payload names the vendor outright",
     "tests": [{"payload": "Manufacturer=AXIS; Model=X", "expect": "AXIS"}]},
    {"name": "onvif_model_rebuilt",
     "source": "onvif_device_info",
     "pattern": r"Model=(?P<v>[^;]+)",
     "target_kind": "field", "target": "model",
     "rationale": "ONVIF key/value payload names the model outright",
     "tests": [{"payload": "Manufacturer=AXIS; Model=X; F=1",
                "expect": "X"}]},
]

AXIS_ALIAS = {"block": "vendor_alias", "surface": "axis",
              "target": "Axis Communications",
              "rationale": "surface form of a canonical vendor"}


def _demo_transport(calls=None):
    """Dispatch on the response schema: the rule compiler asks for `rules`,
    the alias miner for `aliases`."""
    def transport(body):
        if calls is not None:
            calls.append(body)
        props = body["output_config"]["format"]["schema"]["properties"]
        if "rules" in props:
            return _api_response({"rules": REBUILT_RULES, "skipped": []})
        return _api_response({"aliases": [AXIS_ALIAS], "new_vocab": []})
    return transport


TRUNCATED_BANNER_RULE = {
    "name": "http_truncated_axis_vendor",
    "source": "http_banner",
    "pattern": r"Server:\s*(?P<v>Ax)$",
    "target_kind": "field",
    "target": "vendor",
    "rationale": "truncated Axis banner; vendor prefix is still present",
    "tests": [{"payload": "Server: Ax", "expect": "Ax"}],
}


def test_apply_merges_bumps_version_and_revalidates(base_run, tmp_path):
    work = tmp_path / "rules"
    work.mkdir()
    import shutil
    for name in (*rc.RULE_FILES, rc.VOCAB_FILE, "VERSION"):
        shutil.copy(Path("rules") / name, work / name)
    proposal = rc.propose(
        base_run, "model-a", observations_path=OBS,
        rules_dir=work, out_root=tmp_path / "p",
        cache_dir=tmp_path / "cache",
        transport=_transport({"rules": [TRUNCATED_BANNER_RULE],
                              "skipped": []}))
    before = (work / "VERSION").read_text().strip()
    applied = rc.apply_proposal(proposal, work)
    assert applied == 1
    merged = yaml.safe_load((work / "extraction.yaml").read_text())
    assert TRUNCATED_BANNER_RULE["name"] in merged["rules"]
    after = (work / "VERSION").read_text().strip()
    assert after != before and after.endswith(".0")
    load_rules(work)


def test_rebuild_demo_recovers_metrics_round_over_round(tmp_path):
    out = rebuild_demo.run_demo(
        OBS, "rules", LABELS, "model-a", rounds=1,
        out_root=tmp_path / "demo", cache_dir=tmp_path / "cache",
        transport=_demo_transport())
    demo = json.loads((out / "demo.json").read_text())
    r0, r1 = demo["history"]
    assert r1["rules_applied"] == 2 and r1["aliases_applied"] == 1
    assert r1["backlog"] > 40
    assert set(r1["four_bucket"]) == {"fixed", "broken", "stable_correct",
                                      "stable_incorrect"}
    # Ablated rules extract no model at all (vendor survives via the OUI
    # map, which ablate deliberately keeps) -- so the rebuilt ONVIF model
    # rule must raise model recall from its floor.
    key = "extraction_recall/field:model"
    assert r1["metrics"][key] > r0["metrics"][key]
    assert r1["four_bucket"]["fixed"] > 0
    report = (out / "demo_report.md").read_text()
    assert "round 1" in report and key in report


# --------------------------------------------------------------------------
# structural guards
# --------------------------------------------------------------------------

OFFLINE_MODULES = ("llm_client", "rule_compiler", "alias_miner", "label_qa",
                   "rule_copilot", "merge_assistant", "narrate",
                   "langfuse_sink", "rebuild_demo")


def test_runtime_path_never_imports_any_offline_tool():
    """Invariant #5's shape, extended to every LLM tool: run.py and
    everything it imports stay model-free."""
    sources = [Path("run.py"), *Path("obs_pipeline").glob("*.py")]
    for src in sources:
        text = src.read_text(encoding="utf-8")
        for name in OFFLINE_MODULES:
            assert name not in text, f"{src} references {name}"


def test_label_qa_reads_no_pipeline_output():
    """§4's independence claim, structurally: the sweep is grounded in
    payloads and labels only, so it must not open run bundles -- otherwise
    pipeline opinion could launder into ground-truth proposals (the
    invariant-#6 concern)."""
    text = Path("label_qa.py").read_text(encoding="utf-8")
    for marker in ("resolutions.csv", "entities.csv", "claims.csv",
                   "trace.jsonl", "manifest.json"):
        assert marker not in text, marker
