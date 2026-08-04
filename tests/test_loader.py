# tests/test_loader.py
import shutil

import pytest

from obs_pipeline.loader import CrossFileError, load_rules

RULES = "rules"
OBS = "obs-data/observations.csv"


def test_loads_all_six_files_plus_vocab_and_version():
    rs = load_rules(RULES, OBS)
    assert rs.version == "0.1.0"
    assert set(rs.file_hashes) == {
        "extraction.yaml", "normalization.yaml", "claims.yaml",
        "scoring.yaml", "entity_resolution.yaml", "field_resolution.yaml",
        "canonical_vocab.csv",
    }
    assert rs.rollup.startswith("sha256:")


def test_vocab_is_inside_the_rollup(tmp_path):
    """§6.3: changing the vocabulary changes pipeline output, so it must move
    the rules hash."""
    d = tmp_path / "rules"
    shutil.copytree(RULES, d)
    before = load_rules(d, OBS).rollup
    (d / "canonical_vocab.csv").write_text(
        (d / "canonical_vocab.csv").read_text() + "Ruckus,\n"
    )
    assert load_rules(d, OBS).rollup != before


def test_rollup_changes_when_any_file_changes(tmp_path):
    d = tmp_path / "rules"
    shutil.copytree(RULES, d)
    before = load_rules(d, OBS)
    (d / "scoring.yaml").write_text(
        (d / "scoring.yaml").read_text().replace("b: 0.30", "b: 0.31")
    )
    after = load_rules(d, OBS)
    assert after.rollup != before.rollup
    assert after.file_hashes["scoring.yaml"] != before.file_hashes["scoring.yaml"]
    assert after.file_hashes["claims.yaml"] == before.file_hashes["claims.yaml"]


def test_unknown_link_basis_reference_fails_loudly(tmp_path):
    """§6.1: entity_resolution referencing a link_basis absent from claims.yaml
    must fail at init, not produce an empty cluster three stages downstream."""
    d = tmp_path / "rules"
    shutil.copytree(RULES, d)
    (d / "entity_resolution.yaml").write_text(
        (d / "entity_resolution.yaml").read_text().replace(
            "basis_precedence: [mac, serial, hostname_token]",
            "basis_precedence: [mac, serial, hostname_token, wifi_bssid]",
        )
    )
    with pytest.raises(CrossFileError, match="wifi_bssid"):
        load_rules(d, OBS)


def test_alias_target_outside_vocabulary_fails_loudly(tmp_path):
    """§6.3: an alias pointing at a non-vocabulary vendor is a latent bug that
    would otherwise surface as an unexplained Unknown at resolution time."""
    d = tmp_path / "rules"
    shutil.copytree(RULES, d)
    (d / "normalization.yaml").write_text(
        (d / "normalization.yaml").read_text().replace(
            '"axis": Axis Communications', '"axis": Axis Corp'
        )
    )
    with pytest.raises(CrossFileError, match="Axis Corp"):
        load_rules(d, OBS)


def test_unmapped_source_in_observations_fails_loudly(tmp_path):
    d = tmp_path / "rules"
    shutil.copytree(RULES, d)
    text = (d / "claims.yaml").read_text().replace(
        "  telnet_banner:     {witness_group: telnet}\n", ""
    )
    (d / "claims.yaml").write_text(text)
    with pytest.raises(CrossFileError, match="telnet_banner"):
        load_rules(d, OBS)


def test_stale_version_bump_is_detected(tmp_path):
    """§6.2: rollup moved but VERSION didn't -> version_verified is False."""
    d = tmp_path / "rules"
    shutil.copytree(RULES, d)
    state = tmp_path / "last_rules_state.json"
    first = load_rules(d, OBS, state_path=state)
    assert first.version_verified is True

    (d / "scoring.yaml").write_text(
        (d / "scoring.yaml").read_text().replace("b: 0.30", "b: 0.35")
    )
    second = load_rules(d, OBS, state_path=state)
    assert second.version_verified is False


def test_version_bumped_alongside_rules_verifies(tmp_path):
    d = tmp_path / "rules"
    shutil.copytree(RULES, d)
    state = tmp_path / "last_rules_state.json"
    load_rules(d, OBS, state_path=state)
    (d / "scoring.yaml").write_text(
        (d / "scoring.yaml").read_text().replace("b: 0.30", "b: 0.35")
    )
    (d / "VERSION").write_text("0.2.0\n")
    assert load_rules(d, OBS, state_path=state).version_verified is True
