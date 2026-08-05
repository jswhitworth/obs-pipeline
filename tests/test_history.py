"""§8.6: each run's metrics.jsonl appends to a single history.jsonl keyed by
run_id, so trending doesn't require walking every run bundle. Joined to
manifest.json this yields metrics-by-rule-version directly."""
import json

from run import run_pipeline


def test_history_accumulates_across_runs(tmp_path):
    root = tmp_path / "runs"
    a = run_pipeline("obs-data/observations.csv", "rules", root)
    b = run_pipeline("obs-data/observations.csv", "rules", root)
    rows = [json.loads(line) for line in
            (root / "history.jsonl").read_text().splitlines() if line.strip()]
    run_ids = {r["run_id"] for r in rows}
    assert {a.name, b.name} <= run_ids


def test_every_history_row_carries_run_id_metric_scope_value_n(tmp_path):
    root = tmp_path / "runs"
    run_pipeline("obs-data/observations.csv", "rules", root)
    rows = [json.loads(line) for line in
            (root / "history.jsonl").read_text().splitlines() if line.strip()]
    assert rows
    assert all(set(r) >= {"run_id", "metric", "scope", "value", "n"} for r in rows)


def test_history_joins_to_manifest_for_metrics_by_rule_version(tmp_path):
    """'singleton rate jumped at rules 1.4.0' should fall out of a query."""
    root = tmp_path / "runs"
    d = run_pipeline("obs-data/observations.csv", "rules", root)
    manifest = json.loads((d / "manifest.json").read_text())
    rows = [json.loads(line) for line in
            (root / "history.jsonl").read_text().splitlines() if line.strip()]
    mine = [r for r in rows if r["run_id"] == manifest["run_id"]]
    singleton = next(r for r in mine if r["metric"] == "singleton_rate")
    assert 0.0 <= singleton["value"] <= 1.0
    assert singleton["n"] > 0
