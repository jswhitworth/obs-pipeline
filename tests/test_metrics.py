# tests/test_metrics.py
import json

import pytest

from obs_pipeline.metrics import load_registry, metrics_hash
from run import run_pipeline


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    return run_pipeline("obs-data/observations.csv", "rules",
                        tmp_path_factory.mktemp("runs"))


def _rows(bundle):
    return [json.loads(line) for line in
            (bundle / "metrics.jsonl").read_text().splitlines() if line.strip()]


def test_metrics_are_long_format_one_row_per_measurement(bundle):
    """§8.2: wide format breaks trending the moment a metric is added."""
    rows = _rows(bundle)
    assert rows
    assert all(set(r) >= {"metric", "scope", "value", "n"} for r in rows)


def test_n_is_mandatory_on_every_row(bundle):
    """§8.2: precision of 1.00 at n=3 must not render identically to n=300."""
    assert all(isinstance(r["n"], int) and r["n"] >= 0 for r in _rows(bundle))


def test_scope_carries_the_dimension_without_name_explosion(bundle):
    """§8.2: one metric name works at every granularity -- no
    fill_rate_model_propagated."""
    scopes = {r["scope"] for r in _rows(bundle)}
    assert "global" in scopes
    assert any(s.startswith("field:") for s in scopes)
    assert any(s.startswith("link_basis:") for s in scopes)
    assert not any("_propagated" in r["metric"] for r in _rows(bundle))


def test_a_plain_run_emits_only_label_free_metrics(bundle):
    """§8.1: requires_labels lets a plain run emit the label-free subset
    cleanly rather than writing nulls for metrics it structurally cannot
    compute."""
    registry = load_registry("metrics.yaml")
    for row in _rows(bundle):
        assert registry[row["metric"]]["requires_labels"] is False


def test_no_metric_row_has_a_null_value(bundle):
    assert all(r["value"] is not None for r in _rows(bundle))


def test_field_fill_rate_is_suppressed_for_closed_vocabulary_fields(bundle):
    """§8.3: fill rate is structurally 100% for vendor/device_type since they
    are never null, so it carries no signal there -- known_rate replaces it."""
    rows = _rows(bundle)
    fill_scopes = {r["scope"] for r in rows if r["metric"] == "field_fill_rate"}
    assert "field:vendor" not in fill_scopes
    assert "field:device_type" not in fill_scopes
    assert "field:model" in fill_scopes
    known_scopes = {r["scope"] for r in rows if r["metric"] == "known_rate"}
    assert {"field:vendor", "field:device_type"} <= known_scopes


def test_fill_rate_is_split_by_provenance(bundle):
    """§8.3: 90% model fill means something different if 60% of it arrived by
    propagation."""
    scopes = {r["scope"] for r in _rows(bundle) if r["metric"] == "field_fill_rate"}
    assert "field:model:propagated" in scopes
    assert "field:model:direct" in scopes


def test_vocab_reject_frequency_ranks_the_expansion_queue(bundle):
    """§6.3: the distinct set of vocab_reject values ranked by frequency."""
    rows = [r for r in _rows(bundle) if r["metric"] == "vocab_reject_frequency"]
    values = {r["scope"].split("value:", 1)[1] for r in rows}
    # Lowercase: normalization canonicalises unmapped values to the alias-map
    # key form, so the queue emits exactly what gets pasted into the rules.
    assert {"lts security", "amcrest", "wisenet"} <= values


def test_metrics_hash_is_in_the_manifest_and_separate_from_rules_rollup(bundle):
    """§8.1: 'recall dropped' can never silently mean 'we changed how recall
    is computed'."""
    m = json.loads((bundle / "manifest.json").read_text())
    assert m["metrics_hash"].startswith("sha256:")
    assert m["metrics_hash"] != m["rules_rollup"]


def test_editing_metrics_yaml_does_not_move_the_rules_rollup(bundle, tmp_path):
    """§8.1: a metric definition must not change the rules version."""
    from obs_pipeline.loader import load_rules
    before = load_rules("rules", "obs-data/observations.csv").rollup
    tweaked = tmp_path / "metrics.yaml"
    tweaked.write_text(open("metrics.yaml").read() + "\nnew_metric:\n"
                       "  scope_type: global\n  requires_labels: false\n"
                       "  direction: neutral\n  description: x\n")
    assert metrics_hash(tweaked) != metrics_hash("metrics.yaml")
    assert load_rules("rules", "obs-data/observations.csv").rollup == before


def test_entity_corroboration_rate_observes_cross_observation_agreement(bundle):
    """§2.3 (amended)/FINDINGS §2: the claim-scoped corroboration_rate is
    structurally blind to agreement across an entity's members -- claims are
    keyed by obs_id -- so it reads 0.0 for model even though three entities
    have members agreeing on model through different protocols. The
    entity-scoped rate reads the pooled union that §2.5's re-score priced.

    Hand-derived from the five multi-member entities: model is corroborated
    on E-001 (onvif+mdns), E-002 (snmp+http) and E-066 (http+mdns) -> 3 of
    the 49 entities with a resolved model. firmware on E-001 and E-052
    (onvif+http both) -> 2 of 46; n=46 proves the undecidable E-074 entity
    stays out of the denominator. device_type pools to {port_signature}
    everywhere -> exactly 0.0."""
    rows = _rows(bundle)
    claim_scoped = {r["scope"]: r for r in rows
                    if r["metric"] == "corroboration_rate"}
    entity_scoped = {r["scope"]: r for r in rows
                     if r["metric"] == "entity_corroboration_rate"}

    assert claim_scoped["field:model"]["value"] == 0.0

    # _row rounds every value to 6 decimals; compare against that contract.
    assert entity_scoped["field:model"]["n"] == 49
    assert entity_scoped["field:model"]["value"] == round(3 / 49, 6)
    assert entity_scoped["field:firmware"]["n"] == 46
    assert entity_scoped["field:firmware"]["value"] == round(2 / 46, 6)
    assert entity_scoped["field:device_type"]["value"] == 0.0
    # Field-scoped only: link bases have no entity-level field resolution.
    assert all(s.startswith("field:") for s in entity_scoped)
