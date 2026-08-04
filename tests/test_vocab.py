from pathlib import Path

from obs_pipeline.vocab import Vocab, load_vocab

VOCAB = Path("rules/canonical_vocab.csv")


def test_reads_both_columns_independently():
    v = load_vocab(VOCAB)
    assert "Axis Communications" in v.vendors
    assert "ip_camera" in v.device_types
    assert len(v.vendors) == 30
    assert len(v.device_types) == 17


def test_blanks_are_dropped_not_treated_as_members():
    v = load_vocab(VOCAB)
    assert "" not in v.vendors
    assert "" not in v.device_types


def test_row_position_asserts_no_vendor_device_type_pairing():
    """§6.3: 'Axis Communications,ip_camera' on one row does NOT assert
    that Axis makes IP cameras. A Vocab exposes no pairing API at all."""
    v = load_vocab(VOCAB)
    assert not hasattr(v, "pairs")
    assert not hasattr(v, "device_types_for_vendor")


def test_escape_values_are_present_and_cased_per_column():
    v = load_vocab(VOCAB)
    assert Vocab.VENDOR_UNKNOWN == "Unknown"
    assert Vocab.DEVICE_TYPE_UNKNOWN == "unknown"
    assert Vocab.VENDOR_UNKNOWN in v.vendors
    assert Vocab.DEVICE_TYPE_UNKNOWN in v.device_types
