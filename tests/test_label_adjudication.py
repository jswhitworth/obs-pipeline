from label_tools import (
    BASIS_PRECEDENCE, resolve_by_basis_precedence, validate_against_vocabulary,
)
from obs_pipeline.loader import load_rules
from obs_pipeline.vocab import load_vocab

RULES = load_rules("rules", "obs-data/observations.csv")
VOCAB = load_vocab("rules/canonical_vocab.csv")


def _l(obs_id, value, basis, labeler="a"):
    return {"obs_id": obs_id, "key_type": "field", "key": "vendor",
            "value": value, "status": "proposed", "label_basis": basis,
            "labeler_certainty": "medium", "blinded": "false",
            "obs_hash": "sha256:x", "labeled_by": labeler, "labeled_at": ""}


def test_precedence_order_matches_the_design():
    assert BASIS_PRECEDENCE == ("physical_inspection", "asset_inventory",
                                "vendor_doc", "payload_inference")


def test_higher_basis_tier_wins_deterministically():
    """§7.2.2: most type-2 disputes resolve without a human meeting."""
    out = resolve_by_basis_precedence([
        _l("OBS-011", "Dahua Technology", "payload_inference", "a"),
        _l("OBS-011", "Hikvision", "physical_inspection", "b"),
    ])
    winner = [r for r in out if r["obs_id"] == "OBS-011"]
    assert len(winner) == 1
    assert winner[0]["value"] == "Hikvision"
    assert winner[0]["status"] == "adjudicated"


def test_same_tier_disagreement_becomes_disputed_not_silently_picked():
    """§7.2.2: a human adjudicator is required only when two labels share the
    same basis tier and disagree."""
    out = resolve_by_basis_precedence([
        _l("OBS-011", "Dahua Technology", "asset_inventory", "a"),
        _l("OBS-011", "Hikvision", "asset_inventory", "b"),
    ])
    assert {r["status"] for r in out} == {"disputed"}
    assert len(out) == 2      # both retained; neither is thrown away


def test_agreeing_labels_at_the_same_tier_are_not_disputed():
    out = resolve_by_basis_precedence([
        _l("OBS-011", "Hikvision", "asset_inventory", "a"),
        _l("OBS-011", "Hikvision", "asset_inventory", "b"),
    ])
    assert len(out) == 1
    assert out[0]["status"] == "agreed"


def test_precedence_is_a_flat_list_not_a_scoring_formula():
    """§7.2.2: resist recursing the claim-scoring math onto labels. Ground
    truth that needs a weighted confidence model is no longer ground truth."""
    import inspect
    src = inspect.getsource(resolve_by_basis_precedence)
    for banned in ["weight", "independence_bonus", "conflict_penalty", "score("]:
        assert banned not in src


def test_out_of_vocabulary_label_is_a_rules_change_request_not_a_label():
    """§7.2.1 kind 1: adjudicating vocabulary disagreement case by case papers
    over a gap in claims.yaml or normalization.yaml."""
    problems = validate_against_vocabulary(
        [_l("OBS-011", "HIKVISION", "payload_inference")], VOCAB, RULES)
    assert problems
    assert "HIKVISION" in problems[0]
    assert "rules change request" in problems[0].lower()


def test_in_vocabulary_label_passes_validation():
    assert validate_against_vocabulary(
        [_l("OBS-011", "Hikvision", "payload_inference")], VOCAB, RULES) == []


def test_open_vocabulary_fields_are_not_constrained():
    """§7.2.1: free text where the vocabulary isn't closed (model), but passed
    through normalization.yaml before comparison."""
    label = {**_l("OBS-011", "DS-2CD2143G0-I", "payload_inference"),
             "key": "model"}
    assert validate_against_vocabulary([label], VOCAB, RULES) == []


def test_the_imported_label_set_is_vocabulary_clean():
    """§6.3: every vendor and device type in the initial label set is present
    in canonical_vocab.csv -- zero genuinely out-of-vocab labels."""
    from label_tools import load_labels
    assert validate_against_vocabulary(load_labels("labels/labels.csv"),
                                       VOCAB, RULES) == []
