"""The §1c rule compiler (free-thinking-llm-options.md item #1).

Every test injects a fake transport -- the suite must never touch the
network, and the transport seam is the compiler's only non-deterministic
edge. Output roots and cache dirs are always tmp paths: propose/compare
default CWD-relative roots (proposals/, llm_cache/), which conftest.py's
session guard now polices alongside runs/ and evals/.
"""
import json

import pytest
import yaml

import rule_compiler as rc
from obs_pipeline.extract import load_observations
from obs_pipeline.loader import load_rules
from run import run_pipeline

OBS = "obs-data/observations.csv"

# A rule the validator should accept for the one real backlog case
# (OBS-044, http_banner, payload "Server: Ax").
GOOD_RULE = {
    "name": "http_truncated_axis_vendor",
    "source": "http_banner",
    "pattern": r"Server:\s*(?P<v>Ax)$",
    "target_kind": "field",
    "target": "vendor",
    "rationale": "truncated Axis banner; vendor prefix is still present",
    "tests": [{"payload": "Server: Ax", "expect": "Ax"}],
}


def _api_response(parsed, stop_reason="end_turn"):
    """The Messages API response shape the transport returns."""
    return {
        "stop_reason": stop_reason,
        "content": [{"type": "text", "text": json.dumps(parsed)}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _transport(parsed, calls=None, stop_reason="end_turn"):
    def transport(body):
        if calls is not None:
            calls.append(body)
        return _api_response(parsed, stop_reason)
    return transport


@pytest.fixture(scope="module")
def base_run(tmp_path_factory):
    return run_pipeline(OBS, "rules", tmp_path_factory.mktemp("runs"))


@pytest.fixture(scope="module")
def good_proposal(base_run, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("prop")
    return rc.propose(
        base_run, "model-a", observations_path=OBS,
        out_root=tmp / "proposals", cache_dir=tmp / "cache",
        transport=_transport({"rules": [GOOD_RULE], "skipped": []}))


def test_backlog_is_collected_from_the_trace(base_run):
    backlog = rc.collect_backlog(base_run, OBS)
    assert [c["obs_id"] for c in backlog] == ["OBS-044"]
    assert backlog[0]["raw_payload"] == "Server: Ax"
    assert backlog[0]["source"] == "http_banner"


def test_cache_key_pins_the_model(base_run):
    """§7 of the options doc: a model upgrade is an explicit, diffable event.
    Same prompt, different model id -> different cache key."""
    rules = load_rules("rules")
    prompt = rc.build_prompt(rc.collect_backlog(base_run, OBS), rules)
    a = rc.cache_key(rc.request_body("model-a", prompt))
    b = rc.cache_key(rc.request_body("model-b", prompt))
    assert a != b
    assert a == rc.cache_key(rc.request_body("model-a", prompt))


def test_cache_hit_never_touches_the_transport(base_run, tmp_path):
    calls = []
    kwargs = dict(observations_path=OBS, out_root=tmp_path / "p",
                  cache_dir=tmp_path / "cache",
                  transport=_transport({"rules": [], "skipped": []}, calls))
    first = rc.propose(base_run, "model-a", **kwargs)
    # Both calls write the same proposal dir, so snapshot before the rerun.
    first_state = json.loads((first / "proposal.json").read_text())
    again = rc.propose(base_run, "model-a", **kwargs)
    assert len(calls) == 1
    assert first_state["cached"] is False
    assert json.loads((again / "proposal.json").read_text())["cached"] is True


def test_empty_backlog_makes_no_api_call(good_proposal, tmp_path):
    """A run whose extraction had no gaps must not spend an API call."""
    cleared = run_pipeline(OBS, good_proposal / "rules", tmp_path / "runs")

    def explode(body):
        raise AssertionError("transport must not be called on empty backlog")

    out = rc.propose(cleared, "model-a", observations_path=OBS,
                     out_root=tmp_path / "p", cache_dir=tmp_path / "cache",
                     transport=explode)
    proposal = json.loads((out / "proposal.json").read_text())
    assert proposal["backlog"] == []
    assert proposal["cache_key"] is None


def test_rejected_rules_never_reach_the_fragment(base_run, tmp_path):
    bad = [
        {**GOOD_RULE, "name": "Bad-Name"},
        {**GOOD_RULE, "name": "bad_regex", "pattern": "("},
        {**GOOD_RULE, "name": "no_v_group", "pattern": "Server: (Ax)"},
        {**GOOD_RULE, "name": "undeclared_target", "target": "nonsense"},
        # collides with an existing extraction.yaml rule name
        {**GOOD_RULE, "name": "onvif_model"},
        {**GOOD_RULE, "name": "test_mismatch",
         "tests": [{"payload": "Server: Ax", "expect": "Axis"}]},
        # valid regex + test, but matches nothing in the backlog: the rule
        # came from somewhere other than the evidence it was asked to cover
        {**GOOD_RULE, "name": "offtopic", "pattern": r"(?P<v>ZZZ)$",
         "tests": [{"payload": "ZZZ", "expect": "ZZZ"}]},
    ]
    out = rc.propose(base_run, "model-a", observations_path=OBS,
                     out_root=tmp_path / "p", cache_dir=tmp_path / "cache",
                     transport=_transport({"rules": bad + [GOOD_RULE],
                                           "skipped": []}))
    proposal = json.loads((out / "proposal.json").read_text())
    assert list(proposal["accepted"]) == [GOOD_RULE["name"]]
    assert {r["rule"]["name"] for r in proposal["rejected"]} == \
        {r["name"] for r in bad}
    for r in proposal["rejected"]:
        assert r["reasons"], r["rule"]["name"]
    fragment = yaml.safe_load((out / "proposed_rules.yaml").read_text())
    assert list(fragment["rules"]) == [GOOD_RULE["name"]]


def test_accepted_rules_carry_provenance(good_proposal):
    """Options doc §7: origin=llm_proposed and the model id ride on every
    model-derived artifact, so 'how much came from the model' stays
    answerable later."""
    fragment = yaml.safe_load((good_proposal / "proposed_rules.yaml").read_text())
    rule = fragment["rules"][GOOD_RULE["name"]]
    assert rule["origin"] == "llm_proposed"
    assert rule["model"] == "model-a"
    proposal = json.loads((good_proposal / "proposal.json").read_text())
    assert proposal["origin"] == "llm_proposed"
    assert proposal["rules_rollup"].startswith("sha256:")


def test_candidate_rules_load_and_clear_the_backlog(good_proposal, tmp_path):
    """The merged candidate dir must survive loader.py's cross-file
    validation, and running the pipeline under it must actually retire the
    backlog case the rule was proposed for."""
    load_rules(good_proposal / "rules")   # raises CrossFileError on failure
    run_dir = run_pipeline(OBS, good_proposal / "rules", tmp_path / "runs")
    ops = [json.loads(line)["op"]
           for line in (run_dir / "trace.jsonl").read_text().splitlines()]
    assert "no_extraction" not in ops


def test_refusal_is_surfaced_not_swallowed(base_run, tmp_path):
    with pytest.raises(RuntimeError, match="declined"):
        rc.propose(base_run, "model-a", observations_path=OBS,
                   out_root=tmp_path / "p", cache_dir=tmp_path / "cache",
                   transport=_transport({}, stop_reason="refusal"))


def test_compare_scores_each_model_with_the_eval_harness(
        base_run, good_proposal, tmp_path):
    # model-b proposes nothing: its candidate rules are the live rules, so
    # its four-bucket diff against the shared baseline must be all-stable.
    empty = rc.propose(base_run, "model-b", observations_path=OBS,
                       out_root=tmp_path / "p", cache_dir=tmp_path / "cache",
                       transport=_transport({"rules": [], "skipped": [
                           {"obs_id": "OBS-044", "reason": "too truncated"}]}))
    out = rc.compare([good_proposal, empty], observations_path=OBS,
                     out_dir=tmp_path / "cmp")
    comparison = json.loads((out / "comparison.json").read_text())
    assert set(comparison["models"]) == {"model-a", "model-b"}
    assert comparison["baseline"]["metrics"]

    a = comparison["models"]["model-a"]
    b = comparison["models"]["model-b"]
    assert a["accepted_rules"] == [GOOD_RULE["name"]]
    assert set(a["four_bucket"]) == {"fixed", "broken", "stable_correct",
                                     "stable_incorrect"}
    assert b["accepted_rules"] == []
    assert b["four_bucket"]["fixed"] == 0 and b["four_bucket"]["broken"] == 0
    md = (out / "comparison.md").read_text()
    assert "model-a" in md and "model-b" in md


def test_runtime_path_never_imports_the_compiler():
    """The mirror of invariant #5's structure: the compiler is offline
    tooling, so nothing on the run.py import path may reach it -- otherwise
    LLM output could enter the deterministic runtime."""
    import pathlib
    sources = [pathlib.Path("run.py"), *pathlib.Path("obs_pipeline").glob("*.py")]
    for src in sources:
        assert "rule_compiler" not in src.read_text(encoding="utf-8"), src
