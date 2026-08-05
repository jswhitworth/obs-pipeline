# obs_pipeline/loader.py
"""Rule loading, cross-file validation and hashing (design doc §6.1, §6.2)."""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from obs_pipeline.vocab import Vocab, load_vocab

RULE_FILES = (
    "extraction.yaml",
    "normalization.yaml",
    "claims.yaml",
    "scoring.yaml",
    "entity_resolution.yaml",
    "field_resolution.yaml",
)
VOCAB_FILE = "canonical_vocab.csv"


class CrossFileError(Exception):
    """A reference in one rule file does not resolve in another (§6.1)."""


@dataclass
class RuleSet:
    extraction: dict
    normalization: dict
    claims: dict
    scoring: dict
    entity_resolution: dict
    field_resolution: dict
    vocab: Vocab
    version: str
    file_hashes: dict[str, str]
    rollup: str
    version_verified: bool = True


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical_file_hash(path: Path) -> str:
    """Hash canonicalized content so line-ending churn is not a rule change."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").strip() + "\n"
    return _sha256_bytes(text.encode("utf-8"))


def _validate(rs: RuleSet, observations_path: str | Path | None) -> None:
    fields = set(rs.claims["fields"])
    bases = set(rs.claims["link_bases"])

    for name, rule in rs.extraction.get("rules", {}).items():
        kind, target = rule["target_kind"], rule["target"]
        pool = fields if kind == "field" else bases
        if target not in pool:
            raise CrossFileError(
                f"extraction.yaml#{name} targets {kind} '{target}', "
                f"not declared in claims.yaml"
            )
        for src in rule["sources"]:
            if src not in rs.claims["sources"]:
                raise CrossFileError(
                    f"extraction.yaml#{name} references source '{src}', "
                    f"not declared in claims.yaml"
                )

    for name, rule in rs.extraction.get("structured", {}).items():
        kind, target = rule["target_kind"], rule["target"]
        pool = fields if kind == "field" else bases
        if target not in pool:
            raise CrossFileError(
                f"extraction.yaml#structured.{name} targets {kind} '{target}', "
                f"not declared in claims.yaml"
            )

    for basis in rs.entity_resolution["basis_precedence"]:
        if basis not in bases:
            raise CrossFileError(
                f"entity_resolution.yaml references link_basis '{basis}', "
                f"not declared in claims.yaml"
            )

    # §6.3: every alias and rebrand target must be a vocabulary member.
    for block, pool, label in (
        ("vendor_alias", rs.vocab.vendors, "vendor"),
        ("oem_rebrand", rs.vocab.vendors, "vendor"),
        ("device_type_alias", rs.vocab.device_types, "device_type"),
    ):
        for surface, target in (rs.normalization.get(block, {}).get("map") or {}).items():
            if target not in pool:
                raise CrossFileError(
                    f"normalization.yaml#{block} maps '{surface}' -> "
                    f"'{target}', not a {label} in canonical_vocab.csv"
                )

    for vendor in rs.extraction.get("oui", {}).get("map", {}).values():
        if vendor not in rs.vocab.vendors:
            raise CrossFileError(
                f"extraction.yaml#oui maps to '{vendor}', "
                f"not a vendor in canonical_vocab.csv"
            )

    for dtype in rs.extraction.get("port_signatures", {}).get("rules", {}):
        if dtype not in rs.vocab.device_types:
            raise CrossFileError(
                f"extraction.yaml#port_signatures declares '{dtype}', "
                f"not a device_type in canonical_vocab.csv"
            )

    for key in (rs.field_resolution.get("conflict_policy", {}).get("per_field") or {}):
        if key not in fields:
            raise CrossFileError(
                f"field_resolution.yaml#conflict_policy.per_field names "
                f"field '{key}', not declared in claims.yaml"
            )

    for key in (rs.field_resolution.get("source_precedence") or {}):
        if key not in fields:
            raise CrossFileError(
                f"field_resolution.yaml#source_precedence names field "
                f"'{key}', not declared in claims.yaml"
            )

    # §6.1/§2.3: scoring.py looks up base_weights[witness_group] and silently
    # defaults to 0.0 on a miss (scoring.py:62) -- there is no code-level
    # signal that the group was unrecognised rather than genuinely absent.
    # A claims.yaml witness_group with no scoring.yaml#base_weights entry
    # would therefore score every claim in that group at 0.0 and pass every
    # other validation, exactly the "empty cluster three stages downstream"
    # failure §6.1 exists to prevent.
    base_weights = rs.scoring.get("base_weights", {})
    for source, cfg in rs.claims.get("sources", {}).items():
        group = cfg["witness_group"]
        if group not in base_weights:
            raise CrossFileError(
                f"claims.yaml#sources.{source} witness_group '{group}' has "
                f"no entry in scoring.yaml#base_weights"
            )

    # §6.3: an escape value is emitted into a closed-vocabulary column, so it
    # must itself be a member of that column's vocabulary -- otherwise
    # fields.py emits a resolved value that canonical_vocab.csv does not
    # recognise as belonging to its own column.
    for field, field_cfg in rs.claims.get("closed_vocabulary_fields", {}).items():
        column = field_cfg["vocab_column"]
        pool = rs.vocab.vendors if column == "vendor" else rs.vocab.device_types
        escape = field_cfg["escape"]
        if escape not in pool:
            raise CrossFileError(
                f"claims.yaml#closed_vocabulary_fields.{field} escape "
                f"'{escape}' is not a {column} in canonical_vocab.csv"
            )

    if observations_path:
        with open(observations_path, newline="", encoding="utf-8") as fh:
            seen = {row["source"] for row in csv.DictReader(fh)}
        for src in sorted(seen):
            if src not in rs.claims["sources"]:
                raise CrossFileError(
                    f"observations.csv contains source '{src}' with no "
                    f"mapping in claims.yaml"
                )


def load_rules(
    rules_dir: str | Path,
    observations_path: str | Path | None = None,
    state_path: str | Path | None = None,
) -> RuleSet:
    d = Path(rules_dir)
    parsed = {
        name: yaml.safe_load((d / name).read_text(encoding="utf-8")) or {}
        for name in RULE_FILES
    }
    hashes = {name: _canonical_file_hash(d / name) for name in RULE_FILES}
    hashes[VOCAB_FILE] = _canonical_file_hash(d / VOCAB_FILE)
    rollup = _sha256_bytes(
        json.dumps([hashes[k] for k in sorted(hashes)], separators=(",", ":")).encode()
    )

    rs = RuleSet(
        extraction=parsed["extraction.yaml"],
        normalization=parsed["normalization.yaml"],
        claims=parsed["claims.yaml"],
        scoring=parsed["scoring.yaml"],
        entity_resolution=parsed["entity_resolution.yaml"],
        field_resolution=parsed["field_resolution.yaml"],
        vocab=load_vocab(d / VOCAB_FILE),
        version=(d / "VERSION").read_text(encoding="utf-8").strip(),
        file_hashes=hashes,
        rollup=rollup,
    )
    _validate(rs, observations_path)

    # §6.2 -- the hash polices the version.
    if state_path:
        sp = Path(state_path)
        if sp.exists():
            prev = json.loads(sp.read_text(encoding="utf-8"))
            if prev["rollup"] != rs.rollup and prev["version"] == rs.version:
                rs.version_verified = False
        sp.write_text(
            json.dumps({"rollup": rs.rollup, "version": rs.version}),
            encoding="utf-8",
        )
    return rs
