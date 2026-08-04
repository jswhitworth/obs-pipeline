# Device Fingerprinting Pipeline — Core Implementation Plan (Phases 1–3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic, fully-traced pipeline that resolves 74 raw device observations into entity records with per-field confidence, and prove the trace is complete by reconstructing every output from `trace.jsonl` alone.

**Architecture:** Values are wrapped in `Traced[T]`, constructible only through `Tracer.step()`, so an untraced value is a type error rather than a missing log line. One module per rule category (§5 of the design doc), with a single shared scoring function that both claim-building and entity resolution import. `replay.py` may import only `trace.py` and `bundle.py`, enforced by test.

**Tech Stack:** Python 3.11.5, PyYAML (`safe_load` only), pytest. Standard library otherwise — no pandas, no networkx.

## Global Constraints

- **Python 3.11.5.** Use `TypeVar` + `Generic[T]`, never PEP 695 `class Foo[T]` syntax.
- **Determinism:** no reliance on dict iteration order, input row order, or wall clock anywhere in the pipeline path. `run_id` carries the only timestamp and is never read by a decision.
- **Every emitted value has a trace step.** No exceptions — absence gets a step too (`no_extraction`, `no_identity_claim`, `merge_refused`, `vocab_reject`, `unmapped_source`).
- **`safe_load` only.** Never `yaml.load`.
- **One scoring function.** `scoring.score()` is the only place the formula appears. Two coefficient *sets*, never two functions.
- **Vendor casing is title-case (`Hikvision`, `HP Inc.`); device_type is snake_case (`ip_camera`). Escape values: `Unknown` for vendor, `unknown` for device_type.** These differ by design (§6.3) — do not normalize them to each other.
- **Never read `labels/` from `run.py` or any module it imports.** Invariant #5.
- Repo root is `/Users/jeffreywhitworth/work/jswhitworth/viakoo.com/obs-pipeline-01`.

## File Structure

```
obs_pipeline/
  __init__.py
  trace.py        Traced[T], Tracer, content-addressed step_id
  vocab.py        column-wise canonical_vocab reader
  loader.py       six-file load, cross-file validation, hashing, VERSION check
  normalize.py    rule category 2
  extract.py      rule category 1
  claims.py       rule category 3
  scoring.py      rule category 4  — the single shared function
  entity.py       rule categories 5 + 6
  fields.py       rule category 7
  confidence.py   harmonic rollup + stability
  bundle.py       run bundle writers
  report.py       REPORT.md render
rules/
  VERSION  extraction.yaml  normalization.yaml  claims.yaml
  scoring.yaml  entity_resolution.yaml  field_resolution.yaml
  canonical_vocab.csv        (moved from vocab/)
run.py  replay.py
tests/
  test_trace.py  test_vocab.py  test_loader.py  test_normalize.py
  test_extract.py  test_claims.py  test_scoring.py  test_entity.py
  test_fields.py  test_confidence.py  test_bundle.py
  test_replay.py  test_regression_cases.py  test_determinism.py
```

---

### Task 1: Scaffold and `trace.py`

Everything downstream imports this, so it lands first.

**Files:**
- Create: `obs_pipeline/__init__.py`, `obs_pipeline/trace.py`, `pytest.ini`, `.gitignore`
- Test: `tests/test_trace.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Traced(value, step_id)`; `Tracer.step(*, op, rule_id=None, inputs=(), output=None, parents=(), **extra) -> Traced`; `Tracer.steps() -> list[dict]` sorted by `step_id`; `canonical_json(obj) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trace.py
from obs_pipeline.trace import Traced, Tracer, canonical_json


def test_step_id_is_content_addressed_not_a_counter():
    a, b = Tracer(), Tracer()
    x = a.step(op="extract", rule_id="extraction.yaml#r1", output="P3245-LVE")
    y = b.step(op="extract", rule_id="extraction.yaml#r1", output="P3245-LVE")
    assert x.step_id == y.step_id
    assert x.step_id.startswith("sha256:")


def test_different_content_yields_different_step_id():
    t = Tracer()
    x = t.step(op="extract", rule_id="extraction.yaml#r1", output="P3245-LVE")
    y = t.step(op="extract", rule_id="extraction.yaml#r1", output="Q6135-LE")
    assert x.step_id != y.step_id


def test_parents_are_recorded_as_step_ids_of_traced_inputs():
    t = Tracer()
    raw = t.step(op="extract", rule_id="extraction.yaml#r1", output="AXIS")
    norm = t.step(op="normalize", rule_id="normalization.yaml#vendor_alias",
                  output="Axis Communications", parents=[raw])
    row = next(s for s in t.steps() if s["step_id"] == norm.step_id)
    assert row["parents"] == [raw.step_id]


def test_identical_steps_are_deduplicated():
    t = Tracer()
    t.step(op="extract", rule_id="r", output="v")
    t.step(op="extract", rule_id="r", output="v")
    assert len(t.steps()) == 1


def test_steps_are_returned_sorted_by_step_id():
    t = Tracer()
    for v in ["c", "a", "b"]:
        t.step(op="extract", rule_id="r", output=v)
    ids = [s["step_id"] for s in t.steps()]
    assert ids == sorted(ids)


def test_canonical_json_is_key_order_independent():
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_trace.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'obs_pipeline'`

- [ ] **Step 3: Write the implementation**

```python
# obs_pipeline/trace.py
"""Content-addressed derivation trace (design doc §9.2, §9.3).

Trace by construction: a Traced value cannot be created except through
Tracer.step(), so a code path that produces a value necessarily emitted a
step for it. See §9.3 -- reconstruction-style tracing drifts from reality
precisely when the code is buggy, which is when the trace is needed.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Generic, Iterable, TypeVar

T = TypeVar("T")


def canonical_json(obj: Any) -> str:
    """Stable JSON: sorted keys, no incidental whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class Traced(Generic[T]):
    """A value plus the id of the step that derived it."""
    value: T
    step_id: str


class Tracer:
    def __init__(self) -> None:
        self._steps: dict[str, dict] = {}

    def step(
        self,
        *,
        op: str,
        rule_id: str | None = None,
        inputs: Iterable[str] = (),
        output: Any = None,
        parents: Iterable[Traced] = (),
        **extra: Any,
    ) -> Traced:
        body: dict[str, Any] = {
            "op": op,
            "rule_id": rule_id,
            "inputs": list(inputs),
            "output": output,
            "parents": [p.step_id for p in parents],
        }
        body.update(extra)
        step_id = "sha256:" + hashlib.sha256(
            canonical_json(body).encode("utf-8")
        ).hexdigest()
        self._steps.setdefault(step_id, {"step_id": step_id, **body})
        return Traced(output, step_id)

    def steps(self) -> list[dict]:
        """All steps, sorted by step_id so two identical runs write identical files."""
        return [self._steps[k] for k in sorted(self._steps)]
```

```ini
# pytest.ini
[pytest]
testpaths = tests
python_files = test_*.py
```

```
# .gitignore
__pycache__/
*.pyc
.pytest_cache/
runs/
evals/
adjudication/
```

`obs_pipeline/__init__.py` is empty.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_trace.py -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Commit**

```bash
git add obs_pipeline/ tests/test_trace.py pytest.ini .gitignore
git commit -m "feat: content-addressed Traced values and Tracer"
```

---

### Task 2: `vocab.py` — column-wise vocabulary

§6.3 warns that a loader treating rows as pairs invents a vendor↔device_type constraint absent from the data. That's the whole test.

**Files:**
- Create: `obs_pipeline/vocab.py`
- Move: `vocab/canonical_vocab.csv` → `rules/canonical_vocab.csv`
- Test: `tests/test_vocab.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `load_vocab(path) -> Vocab`; `Vocab.vendors: frozenset[str]`, `Vocab.device_types: frozenset[str]`, `Vocab.VENDOR_UNKNOWN = "Unknown"`, `Vocab.DEVICE_TYPE_UNKNOWN = "unknown"`.

- [ ] **Step 1: Move the vocabulary into `rules/`**

It must be inside the rules rollup hash (§6.3), because changing it changes pipeline output.

```bash
mkdir -p rules
git mv vocab/canonical_vocab.csv rules/canonical_vocab.csv
rmdir vocab
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_vocab.py
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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_vocab.py -v`
Expected: FAIL — `No module named 'obs_pipeline.vocab'`

- [ ] **Step 4: Write the implementation**

```python
# obs_pipeline/vocab.py
"""Closed value domains for vendor and device_type (design doc §6.3).

The CSV holds two INDEPENDENT column-wise lists padded to a common row
count. Row alignment is an artifact of CSV shape and carries no meaning:
a loader that read rows as pairs would silently invent a vendor->device_type
constraint that is not in the data. This module therefore exposes no pairing
API, so no caller can accidentally rely on one.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Vocab:
    vendors: frozenset[str]
    device_types: frozenset[str]

    VENDOR_UNKNOWN = "Unknown"        # title-case, matching the vendor column
    DEVICE_TYPE_UNKNOWN = "unknown"   # snake_case, matching the device_type column


def load_vocab(path: str | Path) -> Vocab:
    vendors: set[str] = set()
    device_types: set[str] = set()
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            vendor = (row.get("vendor") or "").strip()
            device_type = (row.get("device_type") or "").strip()
            if vendor:
                vendors.add(vendor)
            if device_type:
                device_types.add(device_type)
    return Vocab(frozenset(vendors), frozenset(device_types))
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_vocab.py -v`
Expected: PASS, 4 tests

- [ ] **Step 6: Commit**

```bash
git add obs_pipeline/vocab.py tests/test_vocab.py rules/canonical_vocab.csv
git commit -m "feat: column-wise canonical vocabulary loader"
```

---

### Task 3: Author the six rule files

No code. These are the tuning surface, and every later task reads them.

**Files:**
- Create: `rules/VERSION`, `rules/extraction.yaml`, `rules/normalization.yaml`, `rules/claims.yaml`, `rules/scoring.yaml`, `rules/entity_resolution.yaml`, `rules/field_resolution.yaml`

**Interfaces:**
- Produces: the YAML shapes every later task parses. `rule_id` anchors are `<filename>#<rule name>`.

- [ ] **Step 1: Write `rules/VERSION`**

```
0.1.0
```

- [ ] **Step 2: Write `rules/extraction.yaml`**

Each rule names the source it applies to, a regex over `raw_payload` (or a structured field), and the canonical target key. `target_kind` is `field` or `link_basis`.

```yaml
# extraction.yaml -- rule category 1 (design doc §2.2)
# Each rule: regex over raw_payload with a named group `v`, or a structured
# field lift. `capture_offsets: true` records raw_payload[start:end] in the
# trace per §9.4 rather than copying payload text.
rules:
  onvif_manufacturer:
    sources: [onvif_device_info]
    pattern: 'Manufacturer=(?P<v>[^;]+)'
    target_kind: field
    target: vendor

  onvif_model:
    sources: [onvif_device_info]
    pattern: 'Model=(?P<v>[^;]+)'
    target_kind: field
    target: model

  onvif_firmware:
    sources: [onvif_device_info]
    pattern: 'FirmwareVersion=(?P<v>[^;]+)'
    target_kind: field
    target: firmware

  onvif_serial:
    sources: [onvif_device_info]
    pattern: 'SerialNumber=(?P<v>[A-Za-z0-9]+)'
    target_kind: link_basis
    target: serial

  http_server_vendor:
    sources: [http_banner]
    pattern: 'Server:\s*(?P<v>[A-Za-z0-9][A-Za-z0-9 .\-]*?)[/|,\n]'
    target_kind: field
    target: vendor

  http_title_vendor:
    sources: [http_banner]
    pattern: '<title>(?P<v>[^<]+)</title>'
    target_kind: field
    target: vendor

  http_digest_realm_model:
    sources: [http_banner]
    pattern: 'realm="(?P<v>[A-Z0-9][A-Z0-9\-]{4,})"'
    target_kind: field
    target: model

  http_realm_serial:
    sources: [http_banner]
    pattern: 'realm="[A-Z]+_(?P<v>[0-9A-F]{12})"'
    target_kind: link_basis
    target: serial

  http_x_serial:
    sources: [http_banner]
    pattern: 'X-(?:Device-)?Serial:\s*(?P<v>[0-9A-Za-z]+)'
    target_kind: link_basis
    target: serial

  http_x_model:
    sources: [http_banner]
    pattern: 'X-(?:Device-)?Model:\s*(?P<v>[A-Za-z0-9\-]+)'
    target_kind: field
    target: model

  http_axis_firmware:
    sources: [http_banner]
    pattern: 'Server:\s*Axis/(?P<v>[0-9][0-9.]*)'
    target_kind: field
    target: firmware

  http_firmware_generic:
    sources: [http_banner]
    pattern: '(?:Firmware|fw|X-Firmware:|Version:)\s*(?P<v>[0-9][0-9._]*[0-9])'
    target_kind: field
    target: firmware

  snmp_vendor_leading:
    sources: [snmp_sysdescr]
    pattern: '^(?P<v>[A-Za-z][A-Za-z .\-]*?(?:Technologies|Networks, Inc\.|Automation|Techwin|Systems|Electric|Inc\.)?)\s+[A-Z0-9]'
    target_kind: field
    target: vendor

  snmp_model:
    sources: [snmp_sysdescr]
    pattern: '(?P<v>(?:[A-Z]{1,4}[0-9]{3,4}[A-Za-z0-9\-]*|[A-Z]+-[0-9]{4}[A-Za-z0-9\-]*))'
    target_kind: field
    target: model

  snmp_firmware:
    sources: [snmp_sysdescr]
    pattern: '(?:[Ff]irmware|FW|FW V|Version|rev|kernel JUNOS|V)\s*(?P<v>[0-9][0-9A-Za-z._\-]*)'
    target_kind: field
    target: firmware

  snmp_serial:
    sources: [snmp_sysdescr]
    pattern: 'S/N\s+(?P<v>[A-Z0-9]+)'
    target_kind: link_basis
    target: serial

  mdns_vendor:
    sources: [mdns_txt]
    pattern: 'vendor=(?P<v>[A-Za-z0-9\-]+)'
    target_kind: field
    target: vendor

  mdns_model:
    sources: [mdns_txt]
    pattern: 'model=(?P<v>[A-Za-z0-9\-]+)'
    target_kind: field
    target: model

  mdns_serial:
    sources: [mdns_txt]
    pattern: 'serial=(?P<v>[0-9A-Fa-f]+)'
    target_kind: link_basis
    target: serial

  mdns_macaddress:
    sources: [mdns_txt]
    pattern: 'macaddress=(?P<v>[0-9A-Fa-f]{12})'
    target_kind: link_basis
    target: mac

  mdns_service_vendor:
    sources: [mdns_txt]
    pattern: '_(?P<v>[a-z0-9]+)-video\._tcp'
    target_kind: field
    target: vendor

  mdns_freetext_vendor:
    sources: [mdns_txt]
    pattern: 'local \| (?P<v>[A-Z][A-Za-z0-9]+) '
    target_kind: field
    target: vendor

# Structured-field lifts: not regex over raw_payload, but over the named
# observation column. Applied for every source.
structured:
  mac_column:
    column: mac
    target_kind: link_basis
    target: mac
  hostname_column:
    column: hostname
    target_kind: link_basis
    target: hostname_token

# OUI lookup: first 3 MAC octets -> vendor. Evidence for the `vendor` field
# only; deliberately NOT a link_basis, since an OUI is shared by every device
# a manufacturer ever shipped and would merge unrelated devices wholesale.
oui:
  target_kind: field
  target: vendor
  map:
    "AC:CC:8E": Axis Communications
    "00:16:6C": Hanwha Vision
    "00:02:D1": Vivotek
    "00:18:85": Avigilon
    "C0:56:E3": Hikvision
    "00:07:5F": Bosch Security Systems
    "3C:EF:8C": Dahua Technology
    "00:04:7D": Pelco
    "00:80:F0": i-PRO
    "00:06:8E": HID Global
    "00:0F:E5": Lenel
    "00:80:F9": Software House
    "00:0B:AA": Aiphone
    "7C:1E:B3": 2N
    "00:1A:2F": Cisco Systems
    "3C:8A:B0": Juniper Networks
    "78:45:58": Ubiquiti
    "3C:2A:F4": HP Inc.
    "00:1B:1B": Siemens
    "00:00:BC": Rockwell Automation
    "00:40:AE": Honeywell
    "00:C0:B7": APC
    "00:02:D3": Schneider Electric
    "8C:64:22": BrightSign
    "00:07:4D": Zebra Technologies

# Port signatures -> device_type. Weakest evidence in the system; scoring.yaml
# gives `port_signature` the lowest base_weight for exactly this reason.
port_signatures:
  target_kind: field
  target: device_type
  rules:
    ip_camera:      {all_of: [554], any_of: [80, 443]}
    nvr:            {all_of: [554, 8000]}
    access_control_panel: {any_of: [4050, 3001, 9000]}
    network_switch: {all_of: [22, 161]}
    printer:        {all_of: [9100]}
    plc:            {any_of: [102, 44818]}
    ups:            {all_of: [161], any_of: [80], none_of: [554, 22]}
    intercom:       {all_of: [5060]}
```

- [ ] **Step 3: Write `rules/normalization.yaml`**

The four deliberate alias gaps are called out inline so nobody "fixes" them without reading §6.3.

```yaml
# normalization.yaml -- rule category 2 (design doc §2.2)
mac:
  rule_id_suffix: mac_canonical
  strip_delimiters: [":", "-", ".", " "]
  case: upper
  # Canonical form: 12 uppercase hex chars, no delimiters. Applied to both the
  # `mac` column and mdns macaddress= values so they compare equal.

hostname:
  rule_id_suffix: hostname_canonical
  case: lower
  strip: true
  # Empty hostname yields no claim at all -- see claims.yaml `drop_empty`.

vendor_alias:
  rule_id_suffix: vendor_alias
  # Surface string (lowercased, whitespace-collapsed) -> canonical vocab member.
  # Longest match wins; matching is on the whole normalized surface string.
  map:
    "axis": Axis Communications
    "axis communications": Axis Communications
    "hanwha techwin": Hanwha Vision
    "hanwha vision": Hanwha Vision
    "samsung techwin": Hanwha Vision
    "vivotek": Vivotek
    "vivotek inc.": Vivotek
    "avigilon": Avigilon
    "avigilon corporation": Avigilon
    "hikvision": Hikvision
    "bosch": Bosch Security Systems
    "bosch security systems": Bosch Security Systems
    "dahua": Dahua Technology
    "dahua technology": Dahua Technology
    "pelco": Pelco
    "pelco by schneider electric ip camera web server": Pelco
    "panasonic i-pro sensing solutions": i-PRO
    "i-pro": i-PRO
    "panasonic": Panasonic
    "hid": HID Global
    "hid vertx evo v1000 controller": HID Global
    "lenel": Lenel
    "lenel onguard": Lenel
    "software house": Software House
    "aiphone": Aiphone
    "2n": 2N
    "2n ip verso": 2N
    "cisco": Cisco Systems
    "cisco ios software": Cisco Systems
    "juniper networks": Juniper Networks
    "juniper networks, inc.": Juniper Networks
    "ubiquiti": Ubiquiti
    "hp": HP Inc.
    "hp laserjet enterprise m507dn": HP Inc.
    "siemens": Siemens
    "rockwell automation": Rockwell Automation
    "rockwell automation/allen-bradley": Rockwell Automation
    "honeywell": Honeywell
    "honeywell webs-n4 niagara station": Honeywell
    "apc": APC
    "apc smart-ups srt 5000": APC
    "schneider electric": Schneider Electric
    "schneider electric netbotz 355 rack monitor": Schneider Electric
    "brightsign": BrightSign
    "brightsign xt1144 player": BrightSign
    "zebra technologies": Zebra Technologies
    "genetec": Genetec
    "genetec security center archiver": Genetec
    "milestone": Milestone Systems
    "xprotect": Milestone Systems

  # KNOWN GAPS -- deliberately absent, per design doc §6.3.
  # `LTS Security` (OBS-012), `Amcrest` (OBS-036), `Wisenet` (OBS-004) and
  # `VVTK` (serial prefix, OBS-006/007/040) are real surface strings in the
  # data that do not normalize onto a vocabulary member. Leaving them unmapped
  # is what exercises the vocab_reject / vocab_gap_rate machinery on real
  # input (§8.3) instead of leaving it dead code. Adding them is a §7.3
  # Stage 1 finding, made deliberately and with a VERSION bump -- not a
  # drive-by fix.

oem_rebrand:
  rule_id_suffix: oem_rebrand
  # OEM-to-brand mappings, applied after vendor_alias. Empty for now: no
  # confirmed rebrand relationship in this dataset. `LTS Security` and
  # `Amcrest` are widely believed to be Hikvision/Dahua OEM rebrands
  # respectively, but this artifact contains no evidence of that, and §6.3
  # forbids inventing constraints absent from the data.
  map: {}

device_type_alias:
  rule_id_suffix: device_type_alias
  map:
    "camera": ip_camera
    "network camera": ip_camera
    "ip camera": ip_camera
    "network video recorder": nvr
    "recording server": nvr
    "door controller": door_controller
    "network door controller": door_controller
    "door station": intercom
    "video door station": intercom
    "network horn speaker": ip_speaker
    "ethernet switch": network_switch
    "switch": network_switch
    "rack monitor": environmental_sensor
    "player": digital_signage
```

- [ ] **Step 4: Write `rules/claims.yaml`**

```yaml
# claims.yaml -- rule category 3 (design doc §2.3)
# Canonical key vocabularies. `source` is NEVER folded into a key (invariant #2).
fields: [vendor, model, firmware, device_type]
link_bases: [mac, serial, hostname_token]

closed_vocabulary_fields:
  vendor: {vocab_column: vendor, escape: Unknown}
  device_type: {vocab_column: device_type, escape: unknown}

open_vocabulary_fields: [model, firmware]

# Every distinct `source` in observations.csv must appear here, or the loader
# fails and the pipeline emits `unmapped_source` (§6.3).
sources:
  onvif_device_info: {witness_group: onvif}
  http_banner:       {witness_group: http}
  snmp_sysdescr:     {witness_group: snmp}
  mdns_txt:          {witness_group: mdns}
  telnet_banner:     {witness_group: telnet}

drop_empty: true   # a blank mac/hostname yields no claim, not a claim of ""
```

- [ ] **Step 5: Write `rules/scoring.yaml`**

```yaml
# scoring.yaml -- rule category 4 (design doc §2.3)
#
# ONE function, TWO coefficient sets:
#     independence_bonus(k) = b * (1 - r^(k-1))       k = distinct witness groups
#     claim_weight = clamp(max_base + independence_bonus - conflict_penalty, 0, 1)
#
# These are SEEDED DEFAULTS, not calibrated values. Per §7.4, gates calibrated
# against a same-author label set are provisional; this dataset carries roughly
# 5 positive pairs, so fitting to it would manufacture confidence the data
# cannot support. Calibrate via the §7.3 loop when higher-tier labels exist.

base_weights:
  # Rationale: structured protocol responses beat parsed free text, which beats
  # inference from an identifier, which beats a port guess.
  onvif: 0.85            # structured, vendor-authored key=value
  snmp: 0.70             # authoritative but free-text sysDescr
  mdns: 0.65             # structured txt records, but advertisement-driven
  http: 0.55             # banners are the most spoofable/truncated
  telnet: 0.30           # OBS-043 is control bytes and question marks
  oui: 0.50              # reliable for vendor, says nothing about model
  port_signature: 0.25   # weakest in the system; a guess from open ports
  structured_column: 0.80  # the mac/hostname columns themselves

field_claims:
  b: 0.30      # bonus ceiling: corroboration alone can never buy more than +0.30
  r: 0.50      # saturation: +0.15, +0.225, +0.2625 ... converging on b
  conflict_penalty_per_group: 0.10
  max_conflict_penalty: 0.40

identity_claims:
  b: 0.20      # LOWER than field claims -- corroboration buys less
  r: 0.50
  conflict_penalty_per_group: 0.25   # 2.5x the field-claim penalty
  max_conflict_penalty: 0.60
  # §2.3 blast radius: a wrong field claim corrupts one field on one entity.
  # A wrong identity claim merges two physically different devices, and that
  # error then multiplies across every field on every member via sibling
  # propagation (§2.5). The asymmetry is priced in here -- heavier penalty,
  # smaller bonus -- so identity resolution refuses contested merges rather
  # than optimistically clustering.

stability:
  # §2.6 -- confidence measures strength, stability measures how contested.
  weights: {margin: 0.5, witness_dependence: 0.3, live_conflict: 0.2}
```

- [ ] **Step 6: Write `rules/entity_resolution.yaml`**

```yaml
# entity_resolution.yaml -- rule categories 5 and 6 (design doc §2.4)
link_weight_threshold: 0.55
# Below this, an identity claim does not create a merge edge at all.

merge_order: [link_weight_desc, basis_precedence, obs_id_asc]
# §2.4/§1: merge order changes cluster outcomes, so it is a RULE, not an
# accident of input row order. Pinned here rather than inherited from the CSV.

basis_precedence: [mac, serial, hostname_token]
# Category 6. Used for two jobs: ordering merges above, and deciding
# cross-basis contradictions below.

cross_basis_conflict:
  policy: precedence_wins   # alternative: refuse_and_flag
  record_columns: [basis_agreement, conflict_detail]

edge_weight: min_of_endpoints
# A link is conjunctive -- both observations must genuinely carry the value --
# so the weaker endpoint governs, consistent with the multiplicative reasoning
# in §2.5.

entity_id:
  scheme: content_addressed   # sha256 over the sorted member obs_id set
  prefix: "E-"
  hash_length: 12
  # §2.4: stable across identical runs, CHANGES when membership changes. The
  # eval harness must match on the partition, never on ID strings.
```

- [ ] **Step 7: Write `rules/field_resolution.yaml`**

```yaml
# field_resolution.yaml -- rule category 7 (design doc §2.5)
decay_base: 0.75
# propagated_confidence = source_confidence * link_weight * decay_base^hop
# Multiplicative because a chain is conjunctive -- every link must hold
# independently, and a weak link must discount the result.

max_hops: 3

conflict_policy:
  default: highest_weight_wins
  record_runner_up: true       # §9.1 Q6

  per_field:
    firmware: undecidable
    # §2.5, THE stated exception. Firmware is temporal and the data model is
    # atemporal: two members reporting different firmware are not contradicting
    # each other -- the device was upgraded between scans and both readings
    # were true when taken. Highest-weight-wins would emit a confidently
    # single-valued answer to something legitimately multi-valued over time.
    # Entity-level value becomes `undecidable` and is excluded from Stage 4
    # denominators; per-observation readings stay correct in resolutions.csv.

source_precedence: {}
# §2.5: available for cases where a human genuinely knows better than the
# weights (e.g. "for firmware, ONVIF beats SNMP always"). Empty by default --
# the trace showing WHY the winner won is more auditable than a table.

unknown_handling:
  propagates: false     # §2.5 -- Unknown is absence, not a value to spread
  counts_as_conflict: false
  confidence: 0.0
```

- [ ] **Step 8: Verify all files parse**

Run:
```bash
python3 -c "
import yaml, pathlib
for p in sorted(pathlib.Path('rules').glob('*.yaml')):
    yaml.safe_load(p.read_text()); print('ok', p)
print('VERSION', pathlib.Path('rules/VERSION').read_text().strip())
"
```
Expected: six `ok` lines and `VERSION 0.1.0`

- [ ] **Step 9: Commit**

```bash
git add rules/
git commit -m "feat: author the six rule files with seeded coefficients"
```

---

### Task 4: `loader.py` — validation, hashing, stale-bump check

**Files:**
- Create: `obs_pipeline/loader.py`
- Test: `tests/test_loader.py`

**Interfaces:**
- Consumes: `obs_pipeline.vocab.load_vocab`.
- Produces: `load_rules(rules_dir, observations_path=None) -> RuleSet`; `RuleSet` fields `extraction, normalization, claims, scoring, entity_resolution, field_resolution` (dicts), `vocab: Vocab`, `version: str`, `file_hashes: dict[str,str]`, `rollup: str`, `version_verified: bool`; `CrossFileError(Exception)`; `RULE_FILES: tuple[str, ...]`.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_loader.py -v`
Expected: FAIL — `No module named 'obs_pipeline.loader'`

- [ ] **Step 3: Write the implementation**

```python
# obs_pipeline/loader.py
"""Rule loading, cross-file validation and hashing (design doc §6.1, §6.2)."""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_loader.py -v`
Expected: PASS, 8 tests

- [ ] **Step 5: Commit**

```bash
git add obs_pipeline/loader.py tests/test_loader.py
git commit -m "feat: rule loading with cross-file validation and rollup hashing"
```

---

### Task 5: `normalize.py`

**Files:**
- Create: `obs_pipeline/normalize.py`
- Test: `tests/test_normalize.py`

**Interfaces:**
- Consumes: `Tracer`, `Traced`, `RuleSet`.
- Produces: `normalize_mac(raw, rules, tracer, parent=None) -> Traced[str]`; `normalize_hostname`, `normalize_vendor`, `normalize_device_type` with the same signature. Vendor/device_type return the canonical member, or the **original normalized surface string** when no alias matches — never `Unknown`. Vocabulary rejection happens at field resolution (§6.3), not here.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_normalize.py
from obs_pipeline.loader import load_rules
from obs_pipeline.normalize import (
    normalize_device_type, normalize_hostname, normalize_mac, normalize_vendor,
)
from obs_pipeline.trace import Tracer

RULES = load_rules("rules", "obs-data/observations.csv")


def test_mac_delimiters_and_case_collapse_to_one_form():
    t = Tracer()
    a = normalize_mac("AC:CC:8E:4F:21:A9", RULES, t)
    b = normalize_mac("accc8e4f21a9", RULES, t)          # mdns macaddress= form
    c = normalize_mac("ac-cc-8e-4f-21-a9", RULES, t)
    assert a.value == b.value == c.value == "ACCC8E4F21A9"


def test_hostname_lowercases():
    t = Tracer()
    assert normalize_hostname("HIK-4481", RULES, t).value == "hik-4481"
    assert normalize_hostname("nvr-bldgB-01", RULES, t).value == "nvr-bldgb-01"


def test_vendor_aliases_map_onto_vocabulary():
    t = Tracer()
    for surface, expected in [
        ("AXIS", "Axis Communications"),
        ("Avigilon Corporation", "Avigilon"),
        ("VIVOTEK Inc.", "Vivotek"),
        ("HIKVISION", "Hikvision"),
        ("Hanwha Techwin", "Hanwha Vision"),
        ("Panasonic i-PRO Sensing Solutions", "i-PRO"),
    ]:
        assert normalize_vendor(surface, RULES, t).value == expected


def test_known_alias_gaps_pass_through_unmapped():
    """§6.3: these must NOT normalize -- they are the live vocab_reject path."""
    t = Tracer()
    for surface in ["LTS Security", "Amcrest", "Wisenet", "VVTK"]:
        out = normalize_vendor(surface, RULES, t).value
        assert out not in RULES.vocab.vendors, f"{surface} unexpectedly mapped"
        assert out == surface.lower()


def test_unmapped_spellings_of_one_vendor_collapse_to_one_value():
    """§2.2: get this wrong and two sources spelling the same unknown vendor
    differently register as a CONFLICT and penalise each other, with no
    extraction rule looking broken."""
    t = Tracer()
    variants = {normalize_vendor(s, RULES, t).value
                for s in ["Amcrest", "AMCREST", "amcrest", "  Amcrest  "]}
    assert len(variants) == 1


def test_normalization_never_emits_the_escape_value():
    """Rejection belongs at field resolution, where it is visible in the
    trace and countable (§6.3). Normalization must not pre-empt it."""
    t = Tracer()
    assert normalize_vendor("Amcrest", RULES, t).value != "Unknown"


def test_device_type_alias_maps_to_snake_case():
    t = Tracer()
    assert normalize_device_type("Network Camera", RULES, t).value == "ip_camera"
    assert normalize_device_type("Ethernet Switch", RULES, t).value == "network_switch"


def test_every_normalization_emits_a_step_with_before_and_after():
    t = Tracer()
    out = normalize_vendor("AXIS", RULES, t)
    row = next(s for s in t.steps() if s["step_id"] == out.step_id)
    assert row["op"] == "normalize"
    assert row["before"] == "AXIS"
    assert row["output"] == "Axis Communications"
    assert row["rule_id"] == "normalization.yaml#vendor_alias"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_normalize.py -v`
Expected: FAIL — `No module named 'obs_pipeline.normalize'`

- [ ] **Step 3: Write the implementation**

```python
# obs_pipeline/normalize.py
"""Rule category 2 -- canonicalization (design doc §2.2).

Get this wrong and you get silent false conflicts or false corroboration in
§2.3, with no extraction rule looking broken.

Vendor and device_type normalization returns the alias target when one
matches and the cleaned surface string otherwise. It never returns the escape
value: closed-vocabulary rejection happens at field resolution (§6.3), where
it is visible in the trace and countable in metrics.
"""
from __future__ import annotations

import re

from obs_pipeline.trace import Traced, Tracer

_WS = re.compile(r"\s+")


def _surface_key(value: str) -> str:
    return _WS.sub(" ", value.strip()).lower()


def _emit(tracer, rule_id, before, after, parent):
    return tracer.step(
        op="normalize",
        rule_id=rule_id,
        output=after,
        before=before,
        parents=[parent] if parent is not None else [],
    )


def normalize_mac(raw, rules, tracer: Tracer, parent: Traced | None = None) -> Traced[str]:
    cfg = rules.normalization["mac"]
    out = raw or ""
    for delim in cfg["strip_delimiters"]:
        out = out.replace(delim, "")
    out = out.upper() if cfg["case"] == "upper" else out.lower()
    return _emit(tracer, f"normalization.yaml#{cfg['rule_id_suffix']}", raw, out, parent)


def normalize_hostname(raw, rules, tracer: Tracer, parent: Traced | None = None) -> Traced[str]:
    cfg = rules.normalization["hostname"]
    out = (raw or "").strip() if cfg.get("strip") else (raw or "")
    out = out.lower() if cfg["case"] == "lower" else out
    return _emit(tracer, f"normalization.yaml#{cfg['rule_id_suffix']}", raw, out, parent)


def _alias(block, raw, rules, tracer, parent):
    cfg = rules.normalization[block]
    key = _surface_key(raw or "")
    # Unmapped values fall back to the lookup KEY, not the raw surface string.
    # Returning raw case would make "AMCREST", "Amcrest" and "amcrest" three
    # different values -- three claims that should corroborate would instead
    # register as a conflict and penalise each other (§2.3), which is exactly
    # the silent false-conflict this module exists to prevent. The lowercase
    # form is also precisely the alias-map key a human pastes into
    # normalization.yaml when acting on the vocab_reject queue (§6.3).
    out = (cfg.get("map") or {}).get(key, key)
    step = _emit(tracer, f"normalization.yaml#{cfg['rule_id_suffix']}", raw, out, parent)

    rebrand = (rules.normalization.get("oem_rebrand", {}).get("map") or {})
    if block == "vendor_alias" and _surface_key(out) in rebrand:
        target = rebrand[_surface_key(out)]
        suffix = rules.normalization["oem_rebrand"]["rule_id_suffix"]
        return _emit(tracer, f"normalization.yaml#{suffix}", out, target, step)
    return step


def normalize_vendor(raw, rules, tracer: Tracer, parent: Traced | None = None) -> Traced[str]:
    return _alias("vendor_alias", raw, rules, tracer, parent)


def normalize_device_type(raw, rules, tracer: Tracer, parent: Traced | None = None) -> Traced[str]:
    return _alias("device_type_alias", raw, rules, tracer, parent)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_normalize.py -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add obs_pipeline/normalize.py tests/test_normalize.py
git commit -m "feat: normalization with documented alias gaps preserved"
```

---

### Task 6: `extract.py`

**Files:**
- Create: `obs_pipeline/extract.py`
- Test: `tests/test_extract.py`

**Interfaces:**
- Consumes: `Tracer`, `RuleSet`, `normalize.*`.
- Produces: `Extraction` dataclass with fields `target_kind: str`, `target: str`, `value: str`, `source: str`, `witness_group: str`, `rule_id: str`, `traced: Traced[str]`; `extract_observation(obs: dict, rules, tracer) -> list[Extraction]`; `load_observations(path) -> list[dict]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_extract.py
from obs_pipeline.extract import extract_observation, load_observations
from obs_pipeline.loader import load_rules
from obs_pipeline.trace import Tracer

RULES = load_rules("rules", "obs-data/observations.csv")
OBS = {o["obs_id"]: o for o in load_observations("obs-data/observations.csv")}


def _values(obs_id, target):
    out = extract_observation(OBS[obs_id], RULES, Tracer())
    return {e.value for e in out if e.target == target}


def test_loads_all_74_observations():
    assert len(OBS) == 74


def test_onvif_structured_fields():
    assert _values("OBS-001", "vendor") == {"Axis Communications"}
    assert _values("OBS-001", "model") == {"P3245-LVE"}
    assert _values("OBS-001", "firmware") == {"10.12.114"}
    assert "ACCC8E4F21A9" in _values("OBS-001", "serial")


def test_http_realm_yields_both_model_and_serial():
    assert "AXIS_ACCC8E4F21A9" not in _values("OBS-002", "model")
    assert "ACCC8E4F21A9" in _values("OBS-002", "serial")


def test_mdns_macaddress_normalizes_to_the_mac_column_form():
    """OBS-003 carries macaddress=ACCC8E4F21A9 with no delimiters; the mac
    column carries AC:CC:8E:4F:21:A9. Both must land on one value or the
    §2.3 corroboration is silently lost."""
    assert _values("OBS-003", "mac") == {"ACCC8E4F21A9"}


def test_obs_073_has_no_mac_but_still_yields_identity_evidence():
    """E-066 depends on this: OBS-073's mac column is empty."""
    assert _values("OBS-073", "mac") == set()
    assert "00012E" in _values("OBS-073", "serial")
    assert "hik-2143-c2-03" in _values("OBS-073", "hostname_token")


def test_oui_contributes_vendor_but_never_a_link_basis():
    """An OUI is shared by every device a vendor ever shipped; as a link_basis
    it would merge unrelated devices wholesale."""
    out = extract_observation(OBS["OBS-011"], RULES, Tracer())
    oui = [e for e in out if e.witness_group == "oui"]
    assert oui and all(e.target_kind == "field" and e.target == "vendor" for e in oui)
    assert "Hikvision" in {e.value for e in oui}


def test_port_signature_yields_device_type():
    assert "ip_camera" in _values("OBS-001", "device_type")
    assert "network_switch" in _values("OBS-022", "device_type")


def test_empty_hostname_yields_no_claim_rather_than_a_claim_of_empty():
    assert _values("OBS-044", "hostname_token") == set()


def test_no_extraction_emits_an_explicit_absence_step():
    """§9.3: 'no rule matched' and 'a rule matched and yielded nothing' are
    different failures with different fixes.

    OBS-044 is the genuine dead zone: empty mac, empty hostname, a truncated
    `Server: Ax` that matches no pattern, and port 80 alone, which satisfies
    no port signature."""
    t = Tracer()
    extract_observation(OBS["OBS-044"], RULES, t)
    assert any(s["op"] == "no_extraction" for s in t.steps())


def test_unparseable_payload_still_yields_its_out_of_band_identity():
    """OBS-043's telnet payload is control-byte noise, but its mac column
    reads 00:23:AA:11:04:77. The mac and hostname columns are SCAN METADATA,
    not payload content — a garbage banner does not invalidate the address
    observed on the wire. Dropping it would discard real identity evidence
    and misreport the row as unclusterable in no_identity_claim_rate."""
    out = extract_observation(OBS["OBS-043"], RULES, Tracer())
    assert {e.value for e in out if e.target == "mac"} == {"0023AA110477"}


def test_structured_lifts_apply_to_every_source():
    """The structured block is deliberately source-independent. Filtering it
    by source would make the mac column conditional on payload quality."""
    assert "sources" not in RULES.extraction["structured"]["mac_column"]
    assert "sources" not in RULES.extraction["structured"]["hostname_column"]


def test_extraction_step_records_payload_offsets_not_payload_text():
    """§9.4 volume control."""
    t = Tracer()
    out = extract_observation(OBS["OBS-001"], RULES, t)
    model = next(e for e in out if e.target == "model")
    chain = {s["step_id"]: s for s in t.steps()}
    step = chain[model.traced.step_id]
    while step["op"] != "extract":
        step = chain[step["parents"][0]]
    assert step["inputs"] and step["inputs"][0].startswith("obs:OBS-001#raw_payload[")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_extract.py -v`
Expected: FAIL — `No module named 'obs_pipeline.extract'`

- [ ] **Step 3: Write the implementation**

```python
# obs_pipeline/extract.py
"""Rule category 1 -- extraction (design doc §2.2), with normalization applied
immediately so cross-source comparison in §2.3 is valid."""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from obs_pipeline.normalize import (
    normalize_device_type, normalize_hostname, normalize_mac, normalize_vendor,
)
from obs_pipeline.trace import Traced, Tracer

_NORMALIZERS = {
    ("link_basis", "mac"): normalize_mac,
    ("link_basis", "hostname_token"): normalize_hostname,
    ("field", "vendor"): normalize_vendor,
    ("field", "device_type"): normalize_device_type,
}


@dataclass(frozen=True)
class Extraction:
    target_kind: str
    target: str
    value: str
    source: str
    witness_group: str
    rule_id: str
    traced: Traced


def load_observations(path: str | Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _ports(obs) -> set[int]:
    return {int(p) for p in (obs.get("open_ports") or "").split(",") if p.strip()}


def _finish(kind, target, raw_value, obs, source, group, rule_id, rules, tracer, parent):
    normalizer = _NORMALIZERS.get((kind, target))
    traced = normalizer(raw_value, rules, tracer, parent) if normalizer else parent
    if not traced.value:
        return None
    return Extraction(kind, target, traced.value, source, group, rule_id, traced)


def extract_observation(obs: dict, rules, tracer: Tracer) -> list[Extraction]:
    obs_id, source = obs["obs_id"], obs["source"]
    source_cfg = rules.claims["sources"].get(source)
    if source_cfg is None:
        tracer.step(op="unmapped_source", rule_id="claims.yaml#sources",
                    inputs=[f"obs:{obs_id}"], output=source)
        return []
    group = source_cfg["witness_group"]
    payload = obs.get("raw_payload") or ""
    out: list[Extraction] = []

    for name, rule in rules.extraction.get("rules", {}).items():
        if source not in rule["sources"]:
            continue
        m = re.search(rule["pattern"], payload)
        if not m:
            continue
        rule_id = f"extraction.yaml#{name}"
        step = tracer.step(
            op="extract",
            rule_id=rule_id,
            inputs=[f"obs:{obs_id}#raw_payload[{m.start('v')}:{m.end('v')}]"],
            output=m.group("v").strip(),
        )
        e = _finish(rule["target_kind"], rule["target"], m.group("v").strip(),
                    obs, source, group, rule_id, rules, tracer, step)
        if e:
            out.append(e)

    for name, rule in rules.extraction.get("structured", {}).items():
        raw = (obs.get(rule["column"]) or "").strip()
        if not raw and rules.claims.get("drop_empty", True):
            continue
        rule_id = f"extraction.yaml#{name}"
        step = tracer.step(op="extract", rule_id=rule_id,
                           inputs=[f"obs:{obs_id}#{rule['column']}"], output=raw)
        e = _finish(rule["target_kind"], rule["target"], raw, obs, source,
                    "structured_column", rule_id, rules, tracer, step)
        if e:
            out.append(e)

    oui_cfg = rules.extraction.get("oui", {})
    mac = (obs.get("mac") or "").strip().upper()
    if mac and len(mac) >= 8:
        vendor = oui_cfg.get("map", {}).get(mac[:8])
        if vendor:
            rule_id = "extraction.yaml#oui"
            step = tracer.step(op="extract", rule_id=rule_id,
                               inputs=[f"obs:{obs_id}#mac[0:8]"], output=vendor)
            out.append(Extraction("field", "vendor", vendor, source, "oui",
                                  rule_id, step))

    ps_cfg = rules.extraction.get("port_signatures", {})
    ports = _ports(obs)
    for dtype, spec in ps_cfg.get("rules", {}).items():
        if not all(p in ports for p in spec.get("all_of", [])):
            continue
        if spec.get("any_of") and not any(p in ports for p in spec["any_of"]):
            continue
        if any(p in ports for p in spec.get("none_of", [])):
            continue
        rule_id = f"extraction.yaml#port_signatures.{dtype}"
        step = tracer.step(op="extract", rule_id=rule_id,
                           inputs=[f"obs:{obs_id}#open_ports"], output=dtype)
        out.append(Extraction("field", "device_type", dtype, source,
                              "port_signature", rule_id, step))

    if not out:
        tracer.step(op="no_extraction", rule_id="extraction.yaml",
                    inputs=[f"obs:{obs_id}"], output=None)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_extract.py -v`
Expected: PASS, 10 tests

If a regex in `extraction.yaml` needs adjusting to satisfy a test, edit the YAML — not the test. The tests encode facts about the real payloads.

- [ ] **Step 5: Commit**

```bash
git add obs_pipeline/extract.py tests/test_extract.py rules/extraction.yaml
git commit -m "feat: extraction with payload offsets and absence steps"
```

---

### Task 7: `scoring.py` — the single shared function

Built before its two consumers so neither can grow its own copy.

**Files:**
- Create: `obs_pipeline/scoring.py`
- Test: `tests/test_scoring.py`

**Interfaces:**
- Consumes: `Tracer`.
- Produces: `Coefficients.from_rules(rules, kind) -> Coefficients` where `kind` is `"field_claims"` or `"identity_claims"`; `score(*, key, value, witness_groups, base_weights, conflicting_groups, coeff, tracer, rule_id) -> Traced[float]`; `independence_bonus(k, b, r) -> float`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scoring.py
import inspect

import pytest

from obs_pipeline.loader import load_rules
from obs_pipeline.scoring import Coefficients, independence_bonus, score
from obs_pipeline.trace import Tracer

RULES = load_rules("rules", "obs-data/observations.csv")
FIELD = Coefficients.from_rules(RULES, "field_claims")
IDENT = Coefficients.from_rules(RULES, "identity_claims")


def _score(groups, base, conflicts, coeff):
    return score(key="vendor", value="Hikvision", witness_groups=groups,
                 base_weights=base, conflicting_groups=conflicts,
                 coeff=coeff, tracer=Tracer(), rule_id="scoring.yaml#test").value


def test_single_witness_earns_no_bonus():
    assert independence_bonus(1, b=0.3, r=0.5) == 0.0


def test_bonus_saturates_rather_than_growing_linearly():
    """§2.3: b=0.3, r=0.5 -> +0.15, +0.225, +0.2625, converging on b."""
    b, r = 0.3, 0.5
    assert independence_bonus(2, b, r) == pytest.approx(0.15)
    assert independence_bonus(3, b, r) == pytest.approx(0.225)
    assert independence_bonus(4, b, r) == pytest.approx(0.2625)
    assert independence_bonus(50, b, r) < b


def test_corroboration_can_never_outrank_by_more_than_b():
    """Linear accumulation would let several mediocre sources outrank one
    authoritative source without bound."""
    many_weak = _score(["a", "b", "c", "d", "e"], {"a": 0.3, "b": 0.3, "c": 0.3,
                                                   "d": 0.3, "e": 0.3}, [], FIELD)
    assert many_weak < 0.3 + FIELD.b + 1e-9


def test_weight_uses_max_base_not_mean():
    """Averaging in a weak agreeing witness would LOWER confidence in a value
    that just gained support (§2.3)."""
    strong_alone = _score(["onvif"], {"onvif": 0.85}, [], FIELD)
    strong_plus_weak = _score(["onvif", "http"], {"onvif": 0.85, "http": 0.55}, [], FIELD)
    assert strong_plus_weak > strong_alone


def test_conflict_lowers_the_weight():
    clean = _score(["onvif"], {"onvif": 0.85}, [], FIELD)
    contested = _score(["onvif"], {"onvif": 0.85}, ["http"], FIELD)
    assert contested < clean


def test_identity_coefficients_penalize_conflict_harder_than_field():
    """§2.3 blast radius: a false merge multiplies across every field on every
    member, so identity resolution must refuse contested merges."""
    base, groups, conflict = {"onvif": 0.85}, ["onvif"], ["http"]
    field_drop = _score(groups, base, [], FIELD) - _score(groups, base, conflict, FIELD)
    ident_drop = _score(groups, base, [], IDENT) - _score(groups, base, conflict, IDENT)
    assert ident_drop > field_drop
    assert IDENT.b < FIELD.b


def test_weight_is_clamped_to_unit_interval():
    assert _score(["a"], {"a": 0.99}, [], FIELD) <= 1.0
    assert _score(["a"], {"a": 0.10}, ["b", "c", "d", "e"], IDENT) >= 0.0


def test_formula_lives_in_exactly_one_function():
    """Invariant #3: scoring parity is structural. If a second implementation
    of the formula appears, this test is the tripwire."""
    src = inspect.getsource(score)
    assert "max(" in src and "independence_bonus(" in src
    import obs_pipeline.claims as claims_mod
    import obs_pipeline.entity as entity_mod
    for mod in (claims_mod, entity_mod):
        text = inspect.getsource(mod)
        assert "1 - " not in text.replace("1 - r", ""), \
            f"{mod.__name__} appears to reimplement the bonus formula"


def test_score_links_back_to_the_evidence_it_scored():
    """§9.1 Q1: 'which extraction rule fired, on which substring of which
    raw_payload?' is only answerable if the score step names its evidence.
    Without parents the chain claim -> extraction -> payload span is broken
    and a claim can only be matched to its origin by guessing."""
    t = Tracer()
    ev = t.step(op="extract", rule_id="extraction.yaml#x", output="Hikvision")
    out = score(key="vendor", value="Hikvision", witness_groups=["onvif"],
                base_weights={"onvif": 0.85}, conflicting_groups=[],
                coeff=FIELD, tracer=t, rule_id="scoring.yaml#field_claims",
                parents=[ev])
    row = next(s for s in t.steps() if s["step_id"] == out.step_id)
    assert row["parents"] == [ev.step_id]


def test_score_emits_a_decomposition_step():
    """§9.1 Q3: which source supplied the max base, which groups earned the
    bonus, which conflicts caused the penalty."""
    t = Tracer()
    out = score(key="vendor", value="Hikvision",
                witness_groups=["onvif", "http"],
                base_weights={"onvif": 0.85, "http": 0.55},
                conflicting_groups=["snmp"], coeff=FIELD, tracer=t,
                rule_id="scoring.yaml#field_claims")
    row = next(s for s in t.steps() if s["step_id"] == out.step_id)
    d = row["decomposition"]
    assert d["base_max"] == 0.85
    assert d["base_from"] == "onvif"
    assert sorted(d["witness_groups"]) == ["http", "onvif"]
    assert d["penalty"] > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_scoring.py -v`
Expected: FAIL — `No module named 'obs_pipeline.scoring'`

(`test_formula_lives_in_exactly_one_function` will keep failing until Tasks 8 and 9 land. That is intended — it is the parity tripwire.)

- [ ] **Step 3: Write the implementation**

```python
# obs_pipeline/scoring.py
"""Rule category 4 -- THE shared scoring function (design doc §2.3).

    independence_bonus(k) = b * (1 - r^(k-1))
    claim_weight = clamp(max_base + independence_bonus - conflict_penalty, 0, 1)

This function is used by BOTH claim construction (§2.3) and entity resolution
(§2.4). Invariant #3: the formula shape is identical for both; only the
coefficients differ, to reflect their different error blast-radii. Do not
write a second implementation -- import this one.
"""
from __future__ import annotations

from dataclasses import dataclass

from obs_pipeline.trace import Traced, Tracer


@dataclass(frozen=True)
class Coefficients:
    b: float
    r: float
    conflict_penalty_per_group: float
    max_conflict_penalty: float
    name: str

    @classmethod
    def from_rules(cls, rules, kind: str) -> "Coefficients":
        cfg = rules.scoring[kind]
        return cls(
            b=float(cfg["b"]),
            r=float(cfg["r"]),
            conflict_penalty_per_group=float(cfg["conflict_penalty_per_group"]),
            max_conflict_penalty=float(cfg["max_conflict_penalty"]),
            name=kind,
        )


def independence_bonus(k: int, b: float, r: float) -> float:
    """Saturating, not linear. Caps what corroboration alone can buy."""
    if k < 2:
        return 0.0
    return b * (1.0 - r ** (k - 1))


def score(
    *,
    key: str,
    value: str,
    witness_groups,
    base_weights: dict[str, float],
    conflicting_groups,
    coeff: Coefficients,
    tracer: Tracer,
    rule_id: str,
    parents=(),
) -> Traced[float]:
    groups = sorted(set(witness_groups))
    conflicts = sorted(set(conflicting_groups))

    base_from, base_max = None, 0.0
    for g in groups:
        w = float(base_weights.get(g, 0.0))
        if w > base_max or (w == base_max and base_from is None):
            base_from, base_max = g, w

    bonus = independence_bonus(len(groups), coeff.b, coeff.r)
    penalty = min(
        coeff.max_conflict_penalty,
        coeff.conflict_penalty_per_group * len(conflicts),
    )
    weight = max(0.0, min(1.0, base_max + bonus - penalty))

    return tracer.step(
        op="score",
        rule_id=rule_id,
        output=round(weight, 6),
        parents=parents,
        key=key,
        value=value,
        decomposition={
            "base_max": base_max,
            "base_from": base_from,
            "bonus": round(bonus, 6),
            "witness_groups": groups,
            "penalty": round(penalty, 6),
            "conflicting_groups": conflicts,
            "coefficient_set": coeff.name,
        },
    )
```

- [ ] **Step 4: Run tests, deferring the parity tripwire**

Run: `python3 -m pytest tests/test_scoring.py -v --deselect tests/test_scoring.py::test_formula_lives_in_exactly_one_function`
Expected: PASS, 8 tests

- [ ] **Step 5: Commit**

```bash
git add obs_pipeline/scoring.py tests/test_scoring.py
git commit -m "feat: single shared scoring function with two coefficient sets"
```

---

### Task 8: `claims.py`

**Files:**
- Create: `obs_pipeline/claims.py`
- Test: `tests/test_claims.py`

**Interfaces:**
- Consumes: `extract.Extraction`, `scoring.score`, `scoring.Coefficients`.
- Produces: `Claim` dataclass with `obs_id, kind, key, value, weight, witness_groups: tuple[str,...], sources: tuple[str,...], traced, in_vocab: bool`; `build_claims(observations, rules, tracer) -> list[Claim]`; `field_claims(claims)` and `identity_claims(claims)` filters.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_claims.py
from obs_pipeline.claims import build_claims, field_claims, identity_claims
from obs_pipeline.extract import load_observations
from obs_pipeline.loader import load_rules
from obs_pipeline.trace import Tracer

RULES = load_rules("rules", "obs-data/observations.csv")
OBSERVATIONS = load_observations("obs-data/observations.csv")
CLAIMS = build_claims(OBSERVATIONS, RULES, Tracer())


def _for(obs_id, key):
    return {c.value: c for c in CLAIMS if c.obs_id == obs_id and c.key == key}


def test_claim_key_excludes_source():
    """Invariant #2: source is metadata on the claim, never in the key, so
    multiple sources can collide and either corroborate or contradict."""
    c = _for("OBS-001", "vendor")["Axis Communications"]
    assert c.key == "vendor"
    assert "onvif" in c.witness_groups


def test_multiple_witness_groups_collapse_onto_one_claim():
    """OBS-001: ONVIF Manufacturer=AXIS and the AC:CC:8E OUI both say Axis."""
    axis = _for("OBS-001", "vendor")["Axis Communications"]
    assert set(axis.witness_groups) >= {"onvif", "oui"}
    assert axis.weight > 0.85   # max base plus a real independence bonus


def test_out_of_vocab_claims_are_kept_and_flagged():
    """§6.3: rejecting them at construction would discard the evidence that
    the vocabulary or alias map is incomplete at the moment it is generated.

    The value is lowercase because normalization canonicalises unmapped
    surface strings to the alias-map key form (§2.2) — which is exactly the
    key a human pastes into normalization.yaml when acting on the queue."""
    lts = _for("OBS-012", "vendor")
    assert "lts security" in lts, f"got {sorted(lts)}"
    assert lts["lts security"].in_vocab is False


def test_out_of_vocab_claims_survive_alongside_an_in_vocab_rival():
    """OBS-012 carries three vendor claims: `Hikvision` from its OUI (in
    vocab), plus `app-webs` and `lts security` from the HTTP banner (both
    out of vocab). All three must be KEPT — the rejection happens at field
    resolution, where it is visible in the trace and countable."""
    lts = _for("OBS-012", "vendor")
    assert {v for v, c in lts.items() if c.in_vocab} == {"Hikvision"}
    assert {v for v, c in lts.items() if not c.in_vocab} == {"app-webs", "lts security"}


def test_in_vocab_claims_are_flagged_in_vocab():
    assert _for("OBS-001", "vendor")["Axis Communications"].in_vocab is True


def test_open_vocabulary_fields_are_always_in_vocab():
    """model and firmware are open vocabulary -- enumerating model designators
    is not tractable (§2.5)."""
    assert all(c.in_vocab for c in CLAIMS if c.key in {"model", "firmware"})


def test_identity_and_field_claims_are_separable():
    ident = identity_claims(CLAIMS)
    fields = field_claims(CLAIMS)
    assert {c.key for c in ident} <= {"mac", "serial", "hostname_token"}
    assert {c.key for c in fields} <= {"vendor", "model", "firmware", "device_type"}
    assert len(ident) + len(fields) == len(CLAIMS)


def test_identity_claims_are_scored_with_identity_coefficients():
    t = Tracer()
    claims = build_claims(OBSERVATIONS, RULES, t)
    steps = {s["step_id"]: s for s in t.steps()}
    mac = next(c for c in identity_claims(claims) if c.key == "mac")
    assert steps[mac.traced.step_id]["decomposition"]["coefficient_set"] == "identity_claims"
    vendor = next(c for c in field_claims(claims) if c.key == "vendor")
    assert steps[vendor.traced.step_id]["decomposition"]["coefficient_set"] == "field_claims"


def test_conflicting_values_on_one_key_penalize_each_other():
    """OBS-009 says 'Pelco by Schneider Electric...' and its OUI says Pelco;
    any obs whose sources disagree on vendor must show a penalty."""
    contested = [c for c in CLAIMS
                 if c.key == "vendor" and len(_for(c.obs_id, "vendor")) > 1]
    assert contested, "expected at least one obs with competing vendor claims"
    t = Tracer()
    build_claims(OBSERVATIONS, RULES, t)
    penalties = [s["decomposition"]["penalty"] for s in t.steps()
                 if s["op"] == "score"]
    assert any(p > 0 for p in penalties)


def test_a_claim_is_walkable_back_to_its_payload_span():
    """§9.1 Q1/Q2. The score step must name the extraction/normalization
    steps it scored, so an auditor can walk a claim back to the substring it
    came from rather than searching the trace for a matching output."""
    t = Tracer()
    claims = build_claims(OBSERVATIONS, RULES, t)
    steps = {s["step_id"]: s for s in t.steps()}
    claim = next(c for c in claims if c.obs_id == "OBS-001" and c.key == "model")
    frontier, seen = list(steps[claim.traced.step_id]["parents"]), set()
    spans = []
    while frontier:
        sid = frontier.pop()
        if sid in seen:
            continue
        seen.add(sid)
        step = steps[sid]
        spans += [i for i in step["inputs"] if "#raw_payload[" in i]
        frontier += step["parents"]
    assert any(s.startswith("obs:OBS-001#raw_payload[") for s in spans), spans


def test_obs_with_no_identity_evidence_emits_an_absence_step():
    t = Tracer()
    build_claims(OBSERVATIONS, RULES, t)
    assert any(s["op"] == "no_identity_claim" for s in t.steps())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_claims.py -v`
Expected: FAIL — `No module named 'obs_pipeline.claims'`

- [ ] **Step 3: Write the implementation**

```python
# obs_pipeline/claims.py
"""Rule category 3 -- claim construction (design doc §2.3).

Claims are keyed by (obs_id, key, value). `source` is metadata carried ON the
claim, never folded into the key (invariant #2), so multiple sources can
collide on the same key and either corroborate or contradict as intended.

Claims are NOT vocabulary-constrained; only resolved output is (§6.3). An
out-of-vocab value is kept and flagged, because rejecting it here would
discard the evidence that the vocabulary is incomplete.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from obs_pipeline.extract import extract_observation
from obs_pipeline.scoring import Coefficients, score
from obs_pipeline.trace import Traced, Tracer


@dataclass(frozen=True)
class Claim:
    obs_id: str
    kind: str            # "field" | "link_basis"
    key: str
    value: str
    weight: float
    witness_groups: tuple[str, ...]
    sources: tuple[str, ...]
    traced: Traced
    in_vocab: bool


def field_claims(claims):
    return [c for c in claims if c.kind == "field"]


def identity_claims(claims):
    return [c for c in claims if c.kind == "link_basis"]


def _in_vocab(kind, key, value, rules) -> bool:
    closed = rules.claims.get("closed_vocabulary_fields", {})
    if kind != "field" or key not in closed:
        return True
    column = closed[key]["vocab_column"]
    pool = rules.vocab.vendors if column == "vendor" else rules.vocab.device_types
    return value in pool


def build_claims(observations, rules, tracer: Tracer) -> list[Claim]:
    coeff = {
        "field": Coefficients.from_rules(rules, "field_claims"),
        "link_basis": Coefficients.from_rules(rules, "identity_claims"),
    }
    base_weights = rules.scoring["base_weights"]
    out: list[Claim] = []

    for obs in observations:
        obs_id = obs["obs_id"]
        grouped: dict[tuple[str, str, str], dict] = defaultdict(
            lambda: {"groups": set(), "sources": set(), "parents": []}
        )
        for e in extract_observation(obs, rules, tracer):
            slot = grouped[(e.target_kind, e.target, e.value)]
            slot["groups"].add(e.witness_group)
            slot["sources"].add(e.source)
            slot["parents"].append(e.traced)

        by_key: dict[tuple[str, str], set[str]] = defaultdict(set)
        for (kind, key, value), slot in grouped.items():
            by_key[(kind, key)] |= slot["groups"]

        for (kind, key, value), slot in sorted(grouped.items()):
            # Conflicting = groups on this same key asserting a DIFFERENT value.
            conflicting = by_key[(kind, key)] - slot["groups"]
            traced = score(
                key=key,
                value=value,
                witness_groups=sorted(slot["groups"]),
                base_weights=base_weights,
                conflicting_groups=sorted(conflicting),
                coeff=coeff[kind],
                tracer=tracer,
                rule_id=f"scoring.yaml#{coeff[kind].name}",
                parents=slot["parents"],
            )
            out.append(Claim(
                obs_id=obs_id,
                kind=kind,
                key=key,
                value=value,
                weight=traced.value,
                witness_groups=tuple(sorted(slot["groups"])),
                sources=tuple(sorted(slot["sources"])),
                traced=traced,
                in_vocab=_in_vocab(kind, key, value, rules),
            ))

        if not any(k == "link_basis" for (k, _, _) in grouped):
            tracer.step(op="no_identity_claim", rule_id="claims.yaml#link_bases",
                        inputs=[f"obs:{obs_id}"], output=None)

    return sorted(out, key=lambda c: (c.obs_id, c.kind, c.key, c.value))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_claims.py -v`
Expected: PASS, 9 tests

- [ ] **Step 5: Commit**

```bash
git add obs_pipeline/claims.py tests/test_claims.py
git commit -m "feat: claim construction with source as metadata not key"
```

---

### Task 9: `entity.py` — union-find and basis precedence

**Files:**
- Create: `obs_pipeline/entity.py`
- Test: `tests/test_entity.py`

**Interfaces:**
- Consumes: `claims.Claim`, `claims.identity_claims`, `scoring.score`, `scoring.Coefficients`.
- Produces: `Membership` dataclass with `obs_id, entity_id, link_basis, link_weight, basis_agreement: bool, conflict_detail: str | None, traced`; `resolve_entities(claims, observations, rules, tracer) -> list[Membership]`; `partition(memberships) -> dict[str, frozenset[str]]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_entity.py
import copy

from obs_pipeline.claims import build_claims
from obs_pipeline.entity import partition, resolve_entities
from obs_pipeline.extract import load_observations
from obs_pipeline.loader import load_rules
from obs_pipeline.trace import Tracer

RULES = load_rules("rules", "obs-data/observations.csv")
OBSERVATIONS = load_observations("obs-data/observations.csv")


def _run():
    t = Tracer()
    claims = build_claims(OBSERVATIONS, RULES, t)
    return resolve_entities(claims, OBSERVATIONS, RULES, t), t


MEMBERSHIPS, TRACER = _run()
PARTITION = partition(MEMBERSHIPS)
IDENT = [c for c in build_claims(OBSERVATIONS, RULES, Tracer())
         if c.kind == "link_basis"]


def _cluster_of(obs_id):
    return next(m for eid, m in PARTITION.items() if obs_id in m)


def test_every_observation_lands_in_exactly_one_entity():
    assigned = [m.obs_id for m in MEMBERSHIPS]
    assert len(assigned) == len(set(assigned)) == 74


def test_shared_mac_merges_across_three_sources():
    """E-001 -- OBS-001/002/003, clean corroboration."""
    assert _cluster_of("OBS-001") == {"OBS-001", "OBS-002", "OBS-003"}


def test_shared_mac_merges_despite_differing_hostnames():
    """E-002 -- OBS-004 'nvr-bldgB-01' vs OBS-005 'bldgb-recorder-a'."""
    assert _cluster_of("OBS-004") == {"OBS-004", "OBS-005"}


def test_mac_beats_hostname_and_ip_disagreement():
    """E-052 -- OBS-047/072 share a MAC but differ on hostname AND IP."""
    assert _cluster_of("OBS-047") == {"OBS-047", "OBS-072"}


def test_links_by_serial_when_mac_is_empty():
    """E-066 -- OBS-073 has no MAC at all."""
    assert _cluster_of("OBS-061") == {"OBS-061", "OBS-073"}
    for obs_id in ("OBS-061", "OBS-073"):
        basis = next(m.link_basis for m in MEMBERSHIPS if m.obs_id == obs_id)
        assert basis in {"serial", "hostname_token"}, f"{obs_id} -> {basis}"


def test_link_basis_names_a_claim_that_actually_linked_the_observation():
    """OBS-061 carries a private mac (C056E300012E) that OBS-073 does not
    share, so no mac edge exists. Reporting `mac` because it outranks by
    precedence would misattribute the merge in membership.csv AND point the
    trace parent at evidence that played no part in the decision."""
    for m in MEMBERSHIPS:
        if m.link_basis == "none":
            continue
        siblings = _cluster_of(m.obs_id) - {m.obs_id}
        if not siblings:
            continue
        shared = {c.value for c in IDENT
                  if c.obs_id == m.obs_id and c.key == m.link_basis}
        sibling_values = {c.value for c in IDENT
                          if c.obs_id in siblings and c.key == m.link_basis}
        assert shared & sibling_values, (
            f"{m.obs_id} reports link_basis={m.link_basis} but shares no "
            f"{m.link_basis} value with {sorted(siblings)}"
        )


def test_firmware_conflict_pair_still_merges():
    """E-074 -- OBS-069/074 are one device; the firmware disagreement is
    resolved in §2.5, not by refusing the merge."""
    assert _cluster_of("OBS-069") == {"OBS-069", "OBS-074"}


def test_identical_model_and_vendor_do_not_merge_distinct_devices():
    """OBS-045..052 are eight distinct Axis P3245-LVE cameras with distinct
    MACs and serials. A careless oui_model_pair basis merges them all. §7.3
    Stage 3: bias toward precision -- a false merge corrupts every member's
    fields via propagation."""
    block = [f"OBS-{n:03d}" for n in range(45, 53)]
    clusters = {frozenset(_cluster_of(o)) for o in block}
    assert len(clusters) == 8
    assert _cluster_of("OBS-045") == {"OBS-045"}


def test_hq_l4_east_resolves_to_eight_entities_not_one():
    l4 = {o["obs_id"] for o in OBSERVATIONS if o["site"] == "HQ-L4-East"}
    assert len({frozenset(_cluster_of(o)) for o in l4}) == 8


def test_entity_id_is_content_addressed_and_stable():
    again, _ = _run()
    assert {m.obs_id: m.entity_id for m in MEMBERSHIPS} == \
           {m.obs_id: m.entity_id for m in again}
    assert all(m.entity_id.startswith("E-") for m in MEMBERSHIPS)


def test_entity_id_changes_when_membership_changes():
    """§2.4: IDs are stable across identical runs but CHANGE when membership
    changes -- an ID that silently denoted a different device set would be
    worse than one that changes visibly."""
    singleton = next(m for m in MEMBERSHIPS
                     if len(PARTITION[m.entity_id]) == 1)
    pair = next(m for m in MEMBERSHIPS if len(PARTITION[m.entity_id]) > 1)
    assert singleton.entity_id != pair.entity_id


def test_merge_order_is_pinned_not_inherited_from_row_order():
    """§1/§2.4 determinism."""
    shuffled = list(reversed(OBSERVATIONS))
    t = Tracer()
    claims = build_claims(shuffled, RULES, t)
    other = partition(resolve_entities(claims, shuffled, RULES, t))
    assert {frozenset(v) for v in other.values()} == \
           {frozenset(v) for v in PARTITION.values()}


def test_membership_records_basis_agreement_and_conflict_detail():
    """§2.4: the tie-break decision is recorded as its own auditable columns."""
    m = next(m for m in MEMBERSHIPS if m.obs_id == "OBS-047")
    assert isinstance(m.basis_agreement, bool)
    assert m.conflict_detail is None or isinstance(m.conflict_detail, str)


def test_refused_merges_are_first_class_steps():
    """§9.3: a refused merge is a decision, not a non-event.

    This dataset contains no naturally weak identity claim — every mac,
    serial and hostname_token claim clears 0.55 — so the refusal path is
    exercised by raising the threshold above every claim weight. Asserting
    only against the real data would leave this branch untested, and an
    `or "merge" in ops` escape hatch would make the test vacuous."""
    strict = copy.deepcopy(RULES)
    strict.entity_resolution["link_weight_threshold"] = 0.99
    t = Tracer()
    claims = build_claims(OBSERVATIONS, RULES, t)
    memberships = resolve_entities(claims, OBSERVATIONS, strict, t)
    refused = [s for s in t.steps() if s["op"] == "merge_refused"]
    assert refused, "no merge was refused even at threshold 0.99"
    assert all(s["reason"] == "below_threshold" for s in refused)
    assert len(partition(memberships)) == 74, "every merge should be refused"


def test_no_merge_is_refused_at_the_configured_threshold():
    """The mirror of the above, and a real finding about this data: at the
    configured 0.55 nothing is refused, so cross_basis_conflict_rate and the
    refusal rate are legitimately zero here rather than untested."""
    assert [s for s in TRACER.steps() if s["op"] == "merge_refused"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_entity.py -v`
Expected: FAIL — `No module named 'obs_pipeline.entity'`

- [ ] **Step 3: Write the implementation**

```python
# obs_pipeline/entity.py
"""Rule categories 5 and 6 -- entity resolution (design doc §2.4).

Runs on IDENTITY claims only, never on resolved field values: that keeps the
pipeline a strict DAG and prevents a field->cluster feedback loop (invariant #1).

Merges are applied in the order pinned by entity_resolution.yaml, because
merge order changes cluster outcomes and determinism (§1) requires it be a
rule rather than an accident of input row order.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass

from obs_pipeline.claims import identity_claims
from obs_pipeline.trace import Traced, Tracer


@dataclass(frozen=True)
class Membership:
    obs_id: str
    entity_id: str
    link_basis: str
    link_weight: float
    basis_agreement: bool
    conflict_detail: str | None
    traced: Traced


class _UnionFind:
    def __init__(self, items):
        self.parent = {i: i for i in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        lo, hi = sorted((ra, rb))     # deterministic: lower obs_id becomes root
        self.parent[hi] = lo
        return True


def _entity_id(members, cfg) -> str:
    digest = hashlib.sha256(
        "|".join(sorted(members)).encode("utf-8")
    ).hexdigest()[: cfg["hash_length"]]
    return f"{cfg['prefix']}{digest}"


def partition(memberships) -> dict[str, frozenset[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for m in memberships:
        out[m.entity_id].add(m.obs_id)
    return {k: frozenset(v) for k, v in out.items()}


def _components(edges, obs_ids) -> dict[str, frozenset[str]]:
    """Union-find over ONE subset of edges -> obs_id to its component.

    Used to build a provisional clustering per link_basis, which is what §2.4's
    cross-basis contradiction actually compares. Reading roots out of the main
    union-find mid-loop cannot answer that question: roots keep changing as
    later merges land, and every accepted edge unions unconditionally, so two
    accepted edges touching one observation are always in the same final
    component by construction.
    """
    uf = _UnionFind(obs_ids)
    for edge in edges:
        uf.union(edge[2], edge[3])
    groups: dict[str, set[str]] = defaultdict(set)
    for oid in obs_ids:
        groups[uf.find(oid)].add(oid)
    return {oid: frozenset(groups[uf.find(oid)]) for oid in obs_ids}


def resolve_entities(claims, observations, rules, tracer: Tracer) -> list[Membership]:
    cfg = rules.entity_resolution
    threshold = float(cfg["link_weight_threshold"])
    precedence = list(cfg["basis_precedence"])
    prec_rank = {b: i for i, b in enumerate(precedence)}

    # The YAML is authoritative: fail loudly rather than silently ignoring a
    # value the code does not implement.
    if cfg["edge_weight"] != "min_of_endpoints":
        raise ValueError(
            f"entity_resolution.yaml#edge_weight '{cfg['edge_weight']}' is not "
            f"implemented; only 'min_of_endpoints' is"
        )
    if list(cfg["merge_order"]) != ["link_weight_desc", "basis_precedence", "obs_id_asc"]:
        raise ValueError(
            f"entity_resolution.yaml#merge_order {cfg['merge_order']} does not "
            f"match the implemented order; merge order changes cluster outcomes, "
            f"so a declared order the engine does not honour must not load"
        )
    policy = cfg["cross_basis_conflict"]["policy"]
    if policy not in ("precedence_wins", "refuse_and_flag"):
        raise ValueError(
            f"entity_resolution.yaml#cross_basis_conflict.policy '{policy}' "
            f"is not implemented"
        )

    obs_ids = sorted(o["obs_id"] for o in observations)
    ident = [c for c in identity_claims(claims) if c.key in prec_rank]

    # Candidate edges: two observations sharing one (link_basis, value).
    by_value: dict[tuple[str, str], list] = defaultdict(list)
    for c in ident:
        by_value[(c.key, c.value)].append(c)

    edges = []
    for (basis, value), group in sorted(by_value.items()):
        if len(group) < 2:
            continue
        group = sorted(group, key=lambda c: c.obs_id)
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                weight = min(a.weight, b.weight)   # edge_weight: min_of_endpoints
                edges.append((weight, prec_rank[basis], a.obs_id, b.obs_id,
                              basis, value, a, b))

    # §2.4 pinned merge order: (link_weight desc, basis_precedence, obs_id asc).
    edges.sort(key=lambda e: (-e[0], e[1], e[2], e[3]))

    accepted, uf = [], _UnionFind(obs_ids)
    # Claims that actually produced an accepted edge FOR THIS OBSERVATION.
    # Selecting link_basis from all of an obs's claims instead would let a
    # private claim it shares with nobody outrank the claim that genuinely
    # linked it, misattributing the merge in both membership.csv and the trace.
    linking: dict[str, dict[tuple[str, str], object]] = defaultdict(dict)

    for edge in edges:
        weight, _rank, a_id, b_id, basis, value, a, b = edge
        if weight < threshold:
            tracer.step(op="merge_refused",
                        rule_id="entity_resolution.yaml#link_weight_threshold",
                        inputs=[f"obs:{a_id}", f"obs:{b_id}"],
                        output=None, reason="below_threshold",
                        detail={"basis": basis, "link_weight": round(weight, 6),
                                "threshold": threshold})
            continue
        accepted.append(edge)
        merged = uf.union(a_id, b_id)
        tracer.step(op="merge" if merged else "merge_redundant",
                    rule_id="entity_resolution.yaml#merge_order",
                    inputs=[f"obs:{a_id}", f"obs:{b_id}"],
                    output=None, parents=[a.traced, b.traced],
                    detail={"basis": basis, "value": value,
                            "link_weight": round(weight, 6)})
        linking[a_id][(a.key, a.value)] = a
        linking[b_id][(b.key, b.value)] = b

    groups: dict[str, set[str]] = defaultdict(set)
    for oid in obs_ids:
        groups[uf.find(oid)].add(oid)
    entity_of = {
        oid: _entity_id(members, cfg["entity_id"])
        for members in groups.values()
        for oid in members
    }

    # Provisional clustering per basis, computed AFTER the merge loop.
    per_basis = {
        basis: _components([e for e in accepted if e[4] == basis], obs_ids)
        for basis in precedence
    }

    memberships = []
    for oid in obs_ids:
        candidates = list(linking[oid].values())
        if not candidates:
            candidates = [c for c in ident if c.obs_id == oid]
        # Explicit final tie-break on value: do not rely on upstream sort order.
        candidates.sort(key=lambda c: (-c.weight, prec_rank[c.key], c.value))
        best = candidates[0] if candidates else None

        # A basis only holds an opinion if it actually grouped this obs with
        # someone. Two bases contradict when neither opinion contains the other.
        opinions = {b: comp[oid] for b, comp in per_basis.items()
                    if len(comp[oid]) > 1}
        contending = sorted(
            {b for b, c1 in opinions.items() for b2, c2 in opinions.items()
             if b != b2 and not (c1 <= c2 or c2 <= c1)},
            key=lambda b: prec_rank[b],
        )
        agreement, detail = not contending, None
        if contending:
            winner = contending[0]
            detail = ("cross_basis_conflict: "
                      + ",".join(f"{b}->{sorted(opinions[b])[0]}" for b in contending)
                      + f"; precedence_winner={winner}")
            tracer.step(op="merge_refused",
                        rule_id="entity_resolution.yaml#basis_precedence",
                        inputs=[f"obs:{oid}"], output=None,
                        reason="cross_basis_conflict",
                        detail={"bases": contending, "precedence_winner": winner,
                                "policy": policy})

        traced = tracer.step(
            op="assign_entity",
            rule_id="entity_resolution.yaml#entity_id",
            inputs=[f"obs:{oid}"],
            output=entity_of[oid],
            parents=[best.traced] if best else [],
            detail={"link_basis": best.key if best else None,
                    "basis_agreement": agreement},
        )
        memberships.append(Membership(
            obs_id=oid,
            entity_id=entity_of[oid],
            link_basis=best.key if best else "none",
            link_weight=best.weight if best else 0.0,
            basis_agreement=agreement,
            conflict_detail=detail,
            traced=traced,
        ))
    return memberships
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_entity.py -v`
Expected: PASS, 13 tests

If `test_identical_model_and_vendor_do_not_merge_distinct_devices` fails, the fix belongs in `rules/`, not in the test: confirm `oui_model_pair` is absent from `claims.yaml` `link_bases`, and that no extraction rule targets a `link_basis` shared across distinct devices.

- [ ] **Step 5: Run the scoring parity tripwire, now that both consumers exist**

Run: `python3 -m pytest tests/test_scoring.py::test_formula_lives_in_exactly_one_function -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add obs_pipeline/entity.py tests/test_entity.py
git commit -m "feat: deterministic union-find with pinned merge order"
```

---

### Task 10: `fields.py` — resolution, decay, undecidable firmware

**Files:**
- Create: `obs_pipeline/fields.py`
- Test: `tests/test_fields.py`

**Interfaces:**
- Consumes: `claims.Claim`, `claims.field_claims`, `entity.Membership`.
- Produces: `ResolvedField` dataclass with `entity_id, field, value, confidence, runner_up, runner_up_weight, traced`; `resolve_fields(claims, memberships, rules, tracer) -> dict[str, dict[str, ResolvedField]]` keyed `entity_id -> field -> ResolvedField`; `ObsField` dataclass with `obs_id, field, value, confidence, provenance, traced`; `observation_fields(claims, memberships, resolved, rules, tracer) -> dict[str, dict[str, ObsField]]` keyed `obs_id -> field -> ObsField`; `UNDECIDABLE = "undecidable"`.

**Two levels, deliberately separated.** §2.5 resolves a field *per entity* — the winning claim among all members. §3.1 then asks a different question *per observation*: did **this** payload witness the value, or did it inherit it from a sibling? A single entity-level `provenance` cannot answer that — every member would get the same answer, and OBS-061's own directly-witnessed `vendor` would be marked `propagated` just because its sibling OBS-073 exists. Decay (§2.5) applies at the second level, which is the only place a hop actually occurs.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fields.py
from obs_pipeline.claims import build_claims
from obs_pipeline.entity import partition, resolve_entities
from obs_pipeline.extract import load_observations
from obs_pipeline.fields import UNDECIDABLE, observation_fields, resolve_fields
from obs_pipeline.loader import load_rules
from obs_pipeline.trace import Tracer

RULES = load_rules("rules", "obs-data/observations.csv")
OBSERVATIONS = load_observations("obs-data/observations.csv")
TRACER = Tracer()
CLAIMS = build_claims(OBSERVATIONS, RULES, TRACER)
MEMBERSHIPS = resolve_entities(CLAIMS, OBSERVATIONS, RULES, TRACER)
PARTITION = partition(MEMBERSHIPS)
RESOLVED = resolve_fields(CLAIMS, MEMBERSHIPS, RULES, TRACER)


def _entity_with(obs_id):
    return next(m.entity_id for m in MEMBERSHIPS if m.obs_id == obs_id)


def test_clean_entity_resolves_all_four_fields():
    e = RESOLVED[_entity_with("OBS-001")]
    assert e["vendor"].value == "Axis Communications"
    assert e["model"].value == "P3245-LVE"
    assert e["device_type"].value == "ip_camera"


def test_firmware_conflict_within_entity_is_undecidable():
    """§2.5, the stated exception. OBS-069 reports 8.10.0135 and OBS-074
    reports 8.11.0021 -- the device was upgraded between scans and BOTH
    readings were true when taken. Highest-weight-wins would emit a
    confidently single-valued answer to something multi-valued over time."""
    e = RESOLVED[_entity_with("OBS-069")]
    assert e["firmware"].value == UNDECIDABLE


def test_agreeing_firmware_still_resolves_normally():
    e = RESOLVED[_entity_with("OBS-001")]
    assert e["firmware"].value == "10.12.114"


def test_out_of_vocab_vendor_resolves_to_the_escape_value():
    """§2.5 VOCAB GAP: a value extracted and normalized cleanly, but absent
    from the vocabulary. OBS-033 is the only such case in this data — its
    banner yields `microsoft-httpapi`, which rejects to `Unknown`.

    Note OBS-036 is NOT a vocab-gap case despite carrying `amcrest`: its
    3C:EF:8C OUI supplies an in-vocab `Dahua Technology` that wins, which
    happens to match the label. Out-of-vocab claims losing to an in-vocab
    rival is the design working, not a rejection."""
    e = RESOLVED[_entity_with("OBS-033")]
    assert e["vendor"].value == "Unknown"


def test_no_evidence_and_vocab_gap_both_emit_the_escape_value():
    """§2.5: two distinct situations collapse to the same emitted value but
    stay separable in the trace. OBS-042 has no vendor evidence at all;
    OBS-033 had a value and it was rejected. Only the second is actionable."""
    assert RESOLVED[_entity_with("OBS-042")]["vendor"].value == "Unknown"
    assert RESOLVED[_entity_with("OBS-033")]["vendor"].value == "Unknown"
    rejected_for = {s["output"] for s in TRACER.steps()
                    if s["op"] == "vocab_reject" and s.get("field") == "vendor"}
    assert "microsoft-httpapi" in rejected_for


def test_vocab_reject_is_a_trace_step_not_a_silence():
    """§9.3: without this step the rejected value vanishes and the gap is
    never learned."""
    rejects = [s for s in TRACER.steps() if s["op"] == "vocab_reject"]
    assert rejects
    # Lowercase: normalization canonicalises unmapped surface strings to the
    # alias-map key form, which is the key a human pastes into the rules.
    assert "amcrest" in {s["output"] for s in rejects}


def test_unknown_confidence_is_exactly_zero():
    """§2.5: a non-zero confidence on an absence marker is not interpretable."""
    e = RESOLVED[_entity_with("OBS-033")]
    assert e["vendor"].confidence == 0.0


def test_escape_values_keep_their_per_column_casing():
    e = RESOLVED[_entity_with("OBS-043")]      # telnet, no usable evidence
    assert e["vendor"].value == "Unknown"
    assert e["device_type"].value == "unknown"


def test_closed_vocabulary_fields_are_never_null():
    for fields in RESOLVED.values():
        assert fields["vendor"].value
        assert fields["device_type"].value


def test_provenance_is_per_observation_not_per_entity():
    """§3.1: OBS-001's ONVIF payload states Model=P3245-LVE directly. OBS-002
    is the same device seen over HTTP, whose realm `AXIS_ACCC8E4F21A9` yields
    no model at all — it can only show a model by inheriting one from its
    sibling. A single entity-level provenance cannot tell those apart, and
    Stage 1/2 vs Stage 4 accuracy depend entirely on the distinction.

    Do NOT use OBS-061/073 vendor for this: OBS-073's mDNS payload asserts
    `vendor=HIKVISION` outright, so both members witness vendor directly."""
    obs_view = observation_fields(CLAIMS, MEMBERSHIPS, RESOLVED, RULES, TRACER)
    assert obs_view["OBS-001"]["model"].provenance == "direct"
    assert obs_view["OBS-002"]["model"].provenance == "propagated"
    assert obs_view["OBS-001"]["model"].value == \
           obs_view["OBS-002"]["model"].value == "P3245-LVE"


def test_propagated_confidence_is_decayed_below_the_direct_reading():
    """§2.5: propagated = source_confidence x link_weight x decay_base^hop."""
    obs_view = observation_fields(CLAIMS, MEMBERSHIPS, RESOLVED, RULES, TRACER)
    direct = obs_view["OBS-001"]["model"].confidence
    propagated = obs_view["OBS-002"]["model"].confidence
    assert 0 < propagated < direct


def test_both_members_are_direct_when_both_genuinely_witness_the_field():
    """The mirror case, and a correction to the design doc's §3.1 example:
    OBS-073 does NOT inherit its vendor. Its mDNS payload carries
    `vendor=HIKVISION` explicitly, so both members of E-066 are `direct`."""
    obs_view = observation_fields(CLAIMS, MEMBERSHIPS, RESOLVED, RULES, TRACER)
    assert obs_view["OBS-061"]["vendor"].provenance == "direct"
    assert obs_view["OBS-073"]["vendor"].provenance == "direct"


def test_singleton_members_are_always_direct_or_unknown():
    obs_view = observation_fields(CLAIMS, MEMBERSHIPS, RESOLVED, RULES, TRACER)
    assert obs_view["OBS-045"]["vendor"].provenance == "direct"
    assert obs_view["OBS-043"]["vendor"].provenance == "unknown"


def test_undecidable_entities_keep_their_per_observation_readings():
    """§2.5: the ambiguity exists at the entity level ONLY. OBS-069 saw
    firmware 8.10.0135 and OBS-074 saw 8.11.0021; both were true when taken.
    The entity says `undecidable`, but neither observation may be stripped of
    what it actually witnessed — the design doc states plainly that no
    observation is scored wrong for reporting what it saw."""
    e = RESOLVED[_entity_with("OBS-069")]
    assert e["firmware"].value == UNDECIDABLE

    obs_view = observation_fields(CLAIMS, MEMBERSHIPS, RESOLVED, RULES, TRACER)
    assert obs_view["OBS-069"]["firmware"].value == "8.10.0135"
    assert obs_view["OBS-074"]["firmware"].value == "8.11.0021"
    for obs_id in ("OBS-069", "OBS-074"):
        assert obs_view[obs_id]["firmware"].provenance == "direct"
        assert obs_view[obs_id]["firmware"].confidence > 0.0


def test_unknown_does_not_propagate():
    """§2.5: propagating it would fill a sibling's genuine gap with a
    non-answer that then reads as a resolved field."""
    steps = [s for s in TRACER.steps() if s["op"] == "propagate"]
    assert all(s["output"] not in ("Unknown", "unknown") for s in steps)
    obs_view = observation_fields(CLAIMS, MEMBERSHIPS, RESOLVED, RULES, TRACER)
    for fields in obs_view.values():
        for f in fields.values():
            if f.value in ("Unknown", "unknown", UNDECIDABLE, ""):
                assert f.provenance == "unknown"
                assert f.confidence == 0.0


def test_runner_up_is_recorded_in_the_trace():
    """§9.1 Q6 -- what lost, and by how much."""
    with_runner_up = [f for fields in RESOLVED.values() for f in fields.values()
                      if f.runner_up is not None]
    assert with_runner_up
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fields.py -v`
Expected: FAIL — `No module named 'obs_pipeline.fields'`

- [ ] **Step 3: Write the implementation**

```python
# obs_pipeline/fields.py
"""Rule category 7 -- field resolution (design doc §2.5).

Runs on field claims plus PERSISTED membership. Sibling propagation decays
multiplicatively because a chain is conjunctive: every link must hold
independently, so a low-confidence link discounts appropriately and a
multi-hop path does not inherit full strength from one strong link.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from obs_pipeline.claims import field_claims
from obs_pipeline.trace import Traced, Tracer

UNDECIDABLE = "undecidable"


@dataclass(frozen=True)
class ResolvedField:
    """Entity-level: the winning claim among all members (§2.5)."""
    entity_id: str
    field: str
    value: str
    confidence: float
    runner_up: str | None
    runner_up_weight: float
    witness_groups: tuple[str, ...]
    traced: Traced


@dataclass(frozen=True)
class ObsField:
    """Observation-level: did THIS payload witness the value, or inherit it
    from a sibling (§3.1)? Decay applies here, because this is the only level
    at which a hop actually occurs."""
    obs_id: str
    field: str
    value: str
    confidence: float
    provenance: str          # "direct" | "propagated" | "unknown"
    traced: Traced


def _escape(field: str, rules) -> str:
    return rules.claims["closed_vocabulary_fields"][field]["escape"]


def resolve_fields(claims, memberships, rules, tracer: Tracer):
    cfg = rules.field_resolution
    closed = rules.claims.get("closed_vocabulary_fields", {})
    per_field = cfg["conflict_policy"].get("per_field", {})

    members: dict[str, list[str]] = defaultdict(list)
    for m in memberships:
        members[m.entity_id].append(m.obs_id)

    by_obs: dict[str, list] = defaultdict(list)
    for c in field_claims(claims):
        by_obs[c.obs_id].append(c)

    out: dict[str, dict[str, ResolvedField]] = {}

    for entity_id in sorted(members):
        obs_list = sorted(members[entity_id])
        resolved: dict[str, ResolvedField] = {}

        for field in sorted(rules.claims["fields"]):
            candidates = []
            for oid in obs_list:
                for c in by_obs[oid]:
                    if c.key != field:
                        continue
                    if not c.in_vocab:
                        tracer.step(op="vocab_reject",
                                    rule_id="claims.yaml#closed_vocabulary_fields",
                                    inputs=[f"obs:{oid}"], output=c.value,
                                    parents=[c.traced], field=field)
                        continue
                    candidates.append((c, oid))

            if not candidates:
                value = _escape(field, rules) if field in closed else ""
                traced = tracer.step(op="resolve_field",
                                     rule_id="field_resolution.yaml#unknown_handling",
                                     inputs=[f"entity:{entity_id}"], output=value,
                                     field=field, confidence=0.0)
                resolved[field] = ResolvedField(entity_id, field, value, 0.0,
                                                None, 0.0, (), traced)
                continue

            distinct = {c.value for c, _ in candidates}

            # §2.5 -- firmware is temporal and the data model is atemporal.
            if len(distinct) > 1 and per_field.get(field) == UNDECIDABLE:
                traced = tracer.step(
                    op="resolve_field",
                    rule_id="field_resolution.yaml#conflict_policy.per_field",
                    inputs=[f"entity:{entity_id}"], output=UNDECIDABLE,
                    field=field, confidence=0.0,
                    parents=[c.traced for c, _ in candidates],
                    detail={"reason": "temporal_field_disagreement",
                            "values": sorted(distinct)},
                )
                resolved[field] = ResolvedField(entity_id, field, UNDECIDABLE,
                                                0.0, None, 0.0, (), traced)
                continue

            # §2.5 -- highest claim_weight wins. No decay here: the winner is a
            # direct reading by SOME member, so at entity level hop is 0.
            ranked = sorted(candidates, key=lambda ci: (-ci[0].weight, ci[0].value))
            best_claim, _best_obs = ranked[0]
            runner = next((c for c, _ in ranked if c.value != best_claim.value), None)

            traced = tracer.step(
                op="resolve_field",
                rule_id="field_resolution.yaml#conflict_policy",
                inputs=[f"entity:{entity_id}"], output=best_claim.value,
                parents=[best_claim.traced],
                field=field, confidence=round(best_claim.weight, 6),
                detail={"runner_up": runner.value if runner else None,
                        "runner_up_weight": round(runner.weight, 6) if runner else 0.0},
            )
            resolved[field] = ResolvedField(
                entity_id, field, best_claim.value, round(best_claim.weight, 6),
                runner.value if runner else None,
                round(runner.weight, 6) if runner else 0.0,
                tuple(best_claim.witness_groups), traced,
            )
        out[entity_id] = resolved
    return out


def observation_fields(claims, memberships, resolved, rules, tracer: Tracer):
    """§3.1 -- the per-observation view.

    Did THIS payload witness the value, or inherit it by being clustered with
    a sibling that did? Diffing a propagated row naively against labels would
    credit the pipeline for extraction it never performed, so the eval harness
    computes Stage 1/2 accuracy over `direct` rows and Stage 4 over
    `propagated` rows. That split is only possible if provenance is recorded
    here, per observation, rather than once per entity.
    """
    cfg = rules.field_resolution
    decay_base = float(cfg["decay_base"])
    # UNDECIDABLE is deliberately NOT in this set. It is not an absence
    # marker: it only arises when candidates exist and disagree, so the
    # individual readings are real, in-vocab, directly-witnessed evidence.
    # §2.5 -- "the ambiguity exists at the entity level only... no observation
    # is scored wrong for reporting what it actually saw."
    absent = {"Unknown", "unknown", ""}

    weight_of = {m.obs_id: m.link_weight for m in memberships}
    entity_of = {m.obs_id: m.entity_id for m in memberships}

    own: dict[tuple[str, str], list] = defaultdict(list)
    for c in field_claims(claims):
        if c.in_vocab:
            own[(c.obs_id, c.key)].append(c)

    out: dict[str, dict[str, ObsField]] = {}
    for obs_id in sorted(entity_of):
        fields: dict[str, ObsField] = {}
        for field in sorted(rules.claims["fields"]):
            winner = resolved[entity_of[obs_id]][field]

            if winner.value in absent:
                # §2.5 -- Unknown is an absence marker, not a value to spread.
                traced = tracer.step(op="observation_field",
                                     rule_id="field_resolution.yaml#unknown_handling",
                                     inputs=[f"obs:{obs_id}"], output=winner.value,
                                     parents=[winner.traced], field=field,
                                     provenance="unknown", confidence=0.0)
                fields[field] = ObsField(obs_id, field, winner.value, 0.0,
                                         "unknown", traced)
                continue

            if winner.value == UNDECIDABLE:
                # The entity cannot pick a value, but THIS observation saw
                # something specific and it was true when taken. Report it.
                # Collapsing it to the entity marker would score an
                # observation wrong for reporting what it actually witnessed.
                seen = own[(obs_id, field)]
                if seen:
                    best = max(seen, key=lambda c: (c.weight, c.value))
                    traced = tracer.step(
                        op="observation_field",
                        rule_id="field_resolution.yaml#conflict_policy.per_field",
                        inputs=[f"obs:{obs_id}"], output=best.value,
                        parents=[best.traced], field=field, provenance="direct",
                        confidence=round(best.weight, 6),
                        detail={"entity_value": UNDECIDABLE})
                    fields[field] = ObsField(obs_id, field, best.value,
                                             round(best.weight, 6), "direct", traced)
                else:
                    traced = tracer.step(
                        op="observation_field",
                        rule_id="field_resolution.yaml#conflict_policy.per_field",
                        inputs=[f"obs:{obs_id}"], output=UNDECIDABLE,
                        parents=[winner.traced], field=field,
                        provenance="unknown", confidence=0.0)
                    fields[field] = ObsField(obs_id, field, UNDECIDABLE, 0.0,
                                             "unknown", traced)
                continue

            mine = [c for c in own[(obs_id, field)] if c.value == winner.value]
            if mine:
                best = max(mine, key=lambda c: c.weight)
                traced = tracer.step(op="observation_field",
                                     rule_id="field_resolution.yaml#conflict_policy",
                                     inputs=[f"obs:{obs_id}"], output=winner.value,
                                     parents=[best.traced], field=field,
                                     provenance="direct",
                                     confidence=round(best.weight, 6))
                fields[field] = ObsField(obs_id, field, winner.value,
                                         round(best.weight, 6), "direct", traced)
                continue

            # Inherited from a sibling: conjunctive chain, so multiply (§2.5).
            hop = 1
            conf = round(winner.confidence * max(weight_of.get(obs_id, 0.0), 0.0)
                         * (decay_base ** hop), 6)
            traced = tracer.step(op="propagate",
                                 rule_id="field_resolution.yaml#decay_base",
                                 inputs=[f"obs:{obs_id}"], output=winner.value,
                                 parents=[winner.traced], field=field,
                                 provenance="propagated", confidence=conf,
                                 detail={"hop": hop,
                                         "link_weight": weight_of.get(obs_id, 0.0),
                                         "source_confidence": winner.confidence})
            fields[field] = ObsField(obs_id, field, winner.value, conf,
                                     "propagated", traced)
        out[obs_id] = fields
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_fields.py -v`
Expected: PASS, 11 tests

- [ ] **Step 5: Commit**

```bash
git add obs_pipeline/fields.py tests/test_fields.py
git commit -m "feat: field resolution with decay and undecidable firmware"
```

---

### Task 11: `confidence.py` — harmonic rollup and stability

**Files:**
- Create: `obs_pipeline/confidence.py`
- Test: `tests/test_confidence.py`

**Interfaces:**
- Consumes: `fields.ResolvedField`, `Tracer`.
- Produces: `harmonic_mean(values) -> float`; `entity_confidence(resolved_fields, tracer) -> Traced[float]`; `stability(resolved_fields, rules, tracer) -> Traced[float]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_confidence.py
import pytest

from obs_pipeline.confidence import entity_confidence, harmonic_mean, stability
from obs_pipeline.fields import ResolvedField
from obs_pipeline.loader import load_rules
from obs_pipeline.trace import Tracer

RULES = load_rules("rules", "obs-data/observations.csv")


def _field(name, value, conf, runner=None, runner_w=0.0, groups=("onvif",)):
    """ResolvedField is ENTITY-level and carries no provenance — that lives on
    ObsField, per the two-level split in §2.5/§3.1."""
    t = Tracer()
    return ResolvedField("E-x", name, value, conf, runner, runner_w, groups,
                         t.step(op="resolve_field", output=value))


def test_harmonic_mean_is_dominated_by_its_smallest_input():
    """§2.6: vendor 0.95, model 0.90, device_type 0.30 should NOT read as a
    solid record. Arithmetic mean says 0.72; harmonic says ~0.55 and states
    plainly that something in it is weak."""
    assert harmonic_mean([0.95, 0.90, 0.30]) == pytest.approx(0.55, abs=0.02)
    assert harmonic_mean([0.95, 0.90, 0.30]) < sum([0.95, 0.90, 0.30]) / 3


def test_unknown_fields_are_excluded_not_zeroing_the_rollup():
    """§2.6: fields resolving to Unknown (0.0) are excluded rather than
    zeroing the mean; they are visible through known_rate instead."""
    fields = {
        "vendor": _field("vendor", "Hikvision", 0.9),
        "model": _field("model", "DS-2CD2143G0-I", 0.8),
        "device_type": _field("device_type", "unknown", 0.0),
        "firmware": _field("firmware", "", 0.0),
    }
    out = entity_confidence(fields, Tracer()).value
    assert out == pytest.approx(harmonic_mean([0.9, 0.8]))
    assert out > 0.0


def test_entity_with_no_known_fields_has_zero_confidence():
    fields = {"vendor": _field("vendor", "Unknown", 0.0)}
    assert entity_confidence(fields, Tracer()).value == 0.0


def test_stability_separates_settled_from_knife_edge_results():
    """§2.6: two sources at 0.8 backing Hikvision yield the same CONFIDENCE
    whether the runner-up sat at 0.1 or 0.79 -- magnitude alone cannot tell a
    settled result from a knife-edge one."""
    settled = {"vendor": _field("vendor", "Hikvision", 0.8, "Dahua Technology", 0.10)}
    knife = {"vendor": _field("vendor", "Hikvision", 0.8, "Dahua Technology", 0.79)}
    assert entity_confidence(settled, Tracer()).value == \
           entity_confidence(knife, Tracer()).value
    assert stability(settled, RULES, Tracer()).value > \
           stability(knife, RULES, Tracer()).value


def test_stability_is_not_a_restatement_of_confidence():
    """§2.6 exists because 'a mean cannot express' how contested a result is.
    If stability were derived from confidence it would measure nothing new.
    Same confidence, different witness support -> different stability."""
    lone = {"vendor": _field("vendor", "Hikvision", 0.9, groups=("onvif",))}
    corroborated = {"vendor": _field("vendor", "Hikvision", 0.9,
                                     groups=("onvif", "snmp", "mdns"))}
    assert entity_confidence(lone, Tracer()).value == \
           entity_confidence(corroborated, Tracer()).value
    assert stability(corroborated, RULES, Tracer()).value > \
           stability(lone, RULES, Tracer()).value


def test_a_high_confidence_low_stability_result_is_reachable():
    """§2.6 calls this the early-warning quadrant. If the formulas could not
    produce it, the quadrant analysis in the eval would be vacuous."""
    knife_edge = {
        "vendor": _field("vendor", "Hikvision", 0.92, "Dahua Technology", 0.90,
                         groups=("http",)),
        "model": _field("model", "DS-2CD2143G0-I", 0.88, "IPC-HDW3849H", 0.86,
                        groups=("http",)),
    }
    assert entity_confidence(knife_edge, Tracer()).value >= 0.7
    assert stability(knife_edge, RULES, Tracer()).value < 0.7


def test_entity_with_no_known_fields_has_zero_stability():
    """Mechanically coherent: nothing is known, so nothing is settled. A
    consumer sees confidence 0.0 and stability 0.0 together, which reads as
    'no answer here' rather than 'a contested answer'."""
    assert stability({"vendor": _field("vendor", "Unknown", 0.0)},
                     RULES, Tracer()).value == 0.0


def test_stability_is_bounded_to_unit_interval():
    for runner_w in (0.0, 0.4, 0.79, 0.8):
        s = stability({"vendor": _field("vendor", "X", 0.8, "Y", runner_w)},
                      RULES, Tracer()).value
        assert 0.0 <= s <= 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_confidence.py -v`
Expected: FAIL — `No module named 'obs_pipeline.confidence'`

- [ ] **Step 3: Write the implementation**

```python
# obs_pipeline/confidence.py
"""The confidence model (design doc §2.6).

Three different combining operations, deliberately not conflated:
  - witnesses of the SAME value  -> max + saturating bonus  (scoring.py)
  - a propagation CHAIN          -> multiplicative          (fields.py)
  - DIFFERENT fields into a record -> harmonic mean         (here)

Harmonic mean is dominated by its smallest input, which is the desired
behavior: a record is only as trustworthy as its weakest field. Same reasoning
that makes F1 a harmonic mean of precision and recall.
"""
from __future__ import annotations

from obs_pipeline.trace import Traced, Tracer


def harmonic_mean(values) -> float:
    vals = [v for v in values if v > 0]
    if not vals:
        return 0.0
    return len(vals) / sum(1.0 / v for v in vals)


def entity_confidence(resolved_fields, tracer: Tracer) -> Traced[float]:
    contributing = {
        name: f.confidence
        for name, f in sorted(resolved_fields.items())
        if f.confidence > 0
    }
    value = round(harmonic_mean(contributing.values()), 6)
    return tracer.step(
        op="resolve_entity",
        rule_id="scoring.yaml#confidence_rollup",
        output=value,
        parents=[f.traced for _, f in sorted(resolved_fields.items())],
        detail={"contributing_fields": contributing,
                "excluded_unknown": sorted(set(resolved_fields) - set(contributing))},
    )


def stability(resolved_fields, rules, tracer: Tracer) -> Traced[float]:
    """§2.6 -- how CONTESTED the answer is, which a mean cannot express.

    A result can be high-confidence and low-stability; that combination is the
    early-warning signal for values that will move under recalibration or one
    new observation.
    """
    w = rules.scoring["stability"]["weights"]
    margins, dependence, conflict = [], [], []

    for _, f in sorted(resolved_fields.items()):
        if f.confidence <= 0:
            continue
        margins.append(max(0.0, f.confidence - f.runner_up_weight) / f.confidence)
        # §2.6 -- how many independent witness groups would have to be REMOVED
        # to change the winner. A single-witness value is one retraction away
        # from vanishing; each further independent group makes it harder to
        # overturn, saturating at three.
        #
        # This must be a COUNT, not a rescaled confidence. Deriving it from
        # confidence would make stability a monotone function of confidence,
        # and §2.6's whole claim is that a mean cannot express how contested
        # a result is. A stability that just restates confidence measures
        # nothing, and the §8.4 validation would only re-derive the
        # confidence/accuracy relationship.
        dependence.append(min(1.0, max(0, len(f.witness_groups) - 1) / 2.0))
        conflict.append(0.0 if f.runner_up else 1.0)

    if not margins:
        value = 0.0
    else:
        value = (
            w["margin"] * (sum(margins) / len(margins))
            + w["witness_dependence"] * (sum(dependence) / len(dependence))
            + w["live_conflict"] * (sum(conflict) / len(conflict))
        )
    value = round(max(0.0, min(1.0, value)), 6)
    return tracer.step(
        op="stability",
        rule_id="scoring.yaml#stability",
        output=value,
        parents=[f.traced for _, f in sorted(resolved_fields.items())],
        detail={"weights": dict(w)},
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_confidence.py -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Commit**

```bash
git add obs_pipeline/confidence.py tests/test_confidence.py
git commit -m "feat: harmonic confidence rollup and stability"
```

---

### Task 12: `bundle.py` and `run.py` — the run bundle end to end

**Files:**
- Create: `obs_pipeline/bundle.py`, `run.py`
- Test: `tests/test_bundle.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `write_bundle(run_dir, *, manifest, claims, memberships, resolved, obs_fields, entity_steps, stability_steps, tracer) -> None`; `make_manifest(rules, input_hash, run_id, engine_commit) -> dict` (Task 16 adds a `metrics_hash` parameter); `file_hash(path) -> str`; `run_pipeline(observations_path, rules_dir, out_root) -> Path` (returns the run directory). `entity_steps` and `stability_steps` are both `dict[str, Traced[float]]` keyed by `entity_id`; `obs_fields` is `dict[str, dict[str, ObsField]]` keyed `obs_id -> field`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bundle.py
import csv
import json
from pathlib import Path

import pytest

from run import run_pipeline

BUNDLE = None


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    global BUNDLE
    if BUNDLE is None:
        BUNDLE = run_pipeline("obs-data/observations.csv", "rules",
                              tmp_path_factory.mktemp("runs"))
    return BUNDLE


def _rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_bundle_contains_every_declared_artifact(bundle):
    for name in ["manifest.json", "claims.csv", "membership.csv",
                 "entities.csv", "resolutions.csv", "trace.jsonl"]:
        assert (bundle / name).exists(), name


def test_manifest_records_both_rule_identifiers(bundle):
    m = json.loads((bundle / "manifest.json").read_text())
    assert m["rules_version"] == "0.1.0"
    assert m["rules_rollup"].startswith("sha256:")
    assert set(m["rules_files"]) >= {"scoring.yaml", "canonical_vocab.csv"}
    assert m["input_hash"].startswith("sha256:")
    assert "version_verified" in m


def test_manifest_omits_label_identifiers(bundle):
    """§6.2: run.py never reads labels, so a run manifest asserting a label
    hash would claim knowledge the pipeline structurally cannot have."""
    m = json.loads((bundle / "manifest.json").read_text())
    assert "labels_hash" not in m
    assert "labels_version" not in m


def test_every_row_carries_run_id_and_derivation_step(bundle):
    for name in ["claims.csv", "membership.csv", "entities.csv", "resolutions.csv"]:
        rows = _rows(bundle / name)
        assert rows, name
        for row in rows:
            assert row["run_id"]
            assert row["derivation_step"].startswith("sha256:")


def test_rows_do_not_repeat_rule_hashes(bundle):
    """§3/§6.2: run_id is constant across a run; repeating six hashes on every
    row would be pure duplication."""
    header = _rows(bundle / "membership.csv")[0]
    assert "rules_rollup" not in header
    assert "rules_version" not in header


def test_membership_carries_the_tie_break_columns(bundle):
    rows = _rows(bundle / "membership.csv")
    assert {"obs_id", "entity_id", "link_basis", "link_weight",
            "basis_agreement", "conflict_detail"} <= set(rows[0])
    assert all(r["basis_agreement"] in ("True", "False") for r in rows)


def test_resolutions_is_one_row_per_observation_with_per_field_provenance(bundle):
    rows = _rows(bundle / "resolutions.csv")
    assert len(rows) == 74
    for f in ["vendor", "model", "device_type", "firmware"]:
        assert f"{f}_provenance" in rows[0]
    allowed = {"direct", "propagated", "unknown"}
    assert {r["vendor_provenance"] for r in rows} <= allowed


def test_provenance_differs_between_members_of_one_entity(bundle):
    """§3.1: the whole point of the column. OBS-001's ONVIF payload states
    Model=P3245-LVE; OBS-002 is the same device over HTTP, whose realm yields
    no model, so it can only show one by inheritance. If both rows said the
    same thing, Stage 1/2 and Stage 4 would score the same population.

    Do NOT use OBS-061/073 vendor here — OBS-073's mDNS payload carries
    `vendor=HIKVISION` outright, so both witness it directly."""
    rows = {r["obs_id"]: r for r in _rows(bundle / "resolutions.csv")}
    assert rows["OBS-001"]["entity_id"] == rows["OBS-002"]["entity_id"]
    assert rows["OBS-001"]["model_provenance"] == "direct"
    assert rows["OBS-002"]["model_provenance"] == "propagated"


def test_undecidable_entity_keeps_per_observation_firmware_in_resolutions(bundle):
    """§2.5: the ambiguity is entity-level only. resolutions.csv must show
    what each observation actually saw, not the entity's `undecidable`."""
    rows = {r["obs_id"]: r for r in _rows(bundle / "resolutions.csv")}
    assert rows["OBS-069"]["firmware"] == "8.10.0135"
    assert rows["OBS-074"]["firmware"] == "8.11.0021"
    assert rows["OBS-069"]["firmware_provenance"] == "direct"


def test_resolutions_points_at_the_same_resolve_entity_step_as_the_entity(bundle):
    """§3.1: resolutions.csv is a PURE JOIN VIEW making no new decisions, so
    it emits no trace steps of its own."""
    ents = {r["entity_id"]: r["derivation_step"] for r in _rows(bundle / "entities.csv")}
    for r in _rows(bundle / "resolutions.csv"):
        assert r["derivation_step"] == ents[r["entity_id"]]


def test_entities_carry_per_field_confidence_plus_rollup_and_stability(bundle):
    row = _rows(bundle / "entities.csv")[0]
    for f in ["vendor", "model", "device_type", "firmware"]:
        assert f in row and f"{f}_confidence" in row
    assert "confidence" in row and "stability" in row


def test_engine_commit_records_a_dirty_working_tree(bundle):
    """§6.2: a manifest reporting a clean sha while uncommitted engine code
    ran is worse than omitting the field — precise-looking and wrong."""
    import subprocess
    m = json.loads((bundle / "manifest.json").read_text())
    dirty = subprocess.run(["git", "status", "--porcelain"],
                           capture_output=True, text=True).stdout.strip()
    assert m["engine_commit"].endswith("-dirty") == bool(dirty), (
        f"engine_commit={m['engine_commit']} but working tree "
        f"{'is' if dirty else 'is not'} dirty"
    )


def test_two_runs_in_the_same_second_do_not_clobber_each_other(tmp_path):
    """run_id is second-precision, and runs take a few hundred ms. Without a
    collision suffix an edit-and-rerun inside one second silently destroys
    the earlier bundle."""
    from run import run_pipeline
    root = tmp_path / "runs"
    first = run_pipeline("obs-data/observations.csv", "rules", root)
    second = run_pipeline("obs-data/observations.csv", "rules", root)
    assert first != second, "second run reused the first run's directory"
    assert first.exists() and second.exists()


def test_trace_is_jsonl_with_content_addressed_ids(bundle):
    lines = (bundle / "trace.jsonl").read_text().strip().splitlines()
    assert len(lines) > 500
    ids = [json.loads(line)["step_id"] for line in lines]
    assert all(i.startswith("sha256:") for i in ids)
    assert ids == sorted(ids)


def test_absence_steps_are_present_in_the_trace(bundle):
    """`merge_refused` is deliberately NOT asserted here. Every identity claim
    in this dataset clears the 0.55 threshold and no cross-basis contradiction
    occurs, so a refusal step would only appear if the rules were bent to
    manufacture one. The refusal path is unit-tested in tests/test_entity.py
    against a raised threshold instead."""
    ops = {json.loads(line)["op"]
           for line in (bundle / "trace.jsonl").read_text().splitlines()}
    assert {"no_extraction", "no_identity_claim", "vocab_reject"} <= ops


def test_run_py_never_reads_labels(bundle):
    """Invariant #5, checked against the source rather than asserted.

    The invariant is about READING ground truth, not about the word 'labels'
    appearing -- metrics.py legitimately carries a `requires_labels` flag
    (§8.1). What must not exist is a path that opens a label file or imports
    the label tooling."""
    forbidden = ["labels/", "labels.csv", "labels-initial",
                 "import label_tools", "from label_tools"]
    for p in list(Path("obs_pipeline").glob("*.py")) + [Path("run.py")]:
        text = p.read_text()
        for needle in forbidden:
            assert needle not in text, f"{p} reaches ground truth via '{needle}'"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_bundle.py -v`
Expected: FAIL — `No module named 'run'`

- [ ] **Step 3: Write `obs_pipeline/bundle.py`**

```python
# obs_pipeline/bundle.py
"""Run bundle writers (design doc §3).

Every artifact is machine-readable. Every tabular row carries run_id and
derivation_step; neither rule versions nor derivation chains are repeated per
row -- they resolve through run_id -> manifest.json and derivation_step ->
trace.jsonl, keeping rows narrow while remaining fully traceable.
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

FIELDS = ["vendor", "model", "device_type", "firmware"]


def file_hash(path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_manifest(rules, input_hash: str, run_id: str, engine_commit: str) -> dict:
    return {
        "run_id": run_id,
        "input_hash": input_hash,
        "rules_version": rules.version,
        "rules_rollup": rules.rollup,
        "rules_files": dict(sorted(rules.file_hashes.items())),
        "version_verified": rules.version_verified,
        "engine_commit": engine_commit,
    }


def _write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        for row in rows:
            w.writerow(row)


def write_bundle(run_dir, *, manifest, claims, memberships, resolved, obs_fields,
                 entity_steps, stability_steps, tracer) -> None:
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = manifest["run_id"]

    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    _write_csv(
        run_dir / "claims.csv",
        ["run_id", "derivation_step", "obs_id", "kind", "key", "value",
         "weight", "witness_groups", "sources", "in_vocab"],
        [{"run_id": run_id, "derivation_step": c.traced.step_id,
          "obs_id": c.obs_id, "kind": c.kind, "key": c.key, "value": c.value,
          "weight": c.weight, "witness_groups": "|".join(c.witness_groups),
          "sources": "|".join(c.sources), "in_vocab": c.in_vocab}
         for c in claims],
    )

    _write_csv(
        run_dir / "membership.csv",
        ["run_id", "derivation_step", "obs_id", "entity_id", "link_basis",
         "link_weight", "basis_agreement", "conflict_detail"],
        [{"run_id": run_id, "derivation_step": m.traced.step_id,
          "obs_id": m.obs_id, "entity_id": m.entity_id,
          "link_basis": m.link_basis, "link_weight": m.link_weight,
          "basis_agreement": m.basis_agreement,
          "conflict_detail": m.conflict_detail or ""}
         for m in memberships],
    )

    entity_header = (["run_id", "derivation_step", "entity_id"]
                     + FIELDS + [f"{f}_confidence" for f in FIELDS]
                     + ["confidence", "stability"])
    entity_rows = []
    for entity_id in sorted(resolved):
        fields = resolved[entity_id]
        row = {"run_id": run_id,
               "derivation_step": entity_steps[entity_id].step_id,
               "entity_id": entity_id,
               "confidence": entity_steps[entity_id].value,
               "stability": stability_steps[entity_id].value}
        for f in FIELDS:
            row[f] = fields[f].value
            row[f"{f}_confidence"] = fields[f].confidence
        entity_rows.append(row)
    _write_csv(run_dir / "entities.csv", entity_header, entity_rows)

    # §3.1 -- a PURE JOIN VIEW. It makes no new decisions, so it emits no trace
    # steps and points at the same resolve_entity step as the entity row.
    res_header = (["obs_id", "run_id", "derivation_step", "entity_id"]
                  + [c for f in FIELDS for c in (f, f"{f}_provenance")]
                  + ["confidence", "stability"])
    res_rows = []
    for m in sorted(memberships, key=lambda m: m.obs_id):
        # Per-OBSERVATION provenance (§3.1), not the entity's -- OBS-061
        # witnessed its vendor directly while its sibling OBS-073 inherited it.
        fields = obs_fields[m.obs_id]
        row = {"obs_id": m.obs_id, "run_id": run_id,
               "derivation_step": entity_steps[m.entity_id].step_id,
               "entity_id": m.entity_id,
               "confidence": entity_steps[m.entity_id].value,
               "stability": stability_steps[m.entity_id].value}
        for f in FIELDS:
            row[f] = fields[f].value
            row[f"{f}_provenance"] = fields[f].provenance
        res_rows.append(row)
    _write_csv(run_dir / "resolutions.csv", res_header, res_rows)

    with open(run_dir / "trace.jsonl", "w", encoding="utf-8") as fh:
        for step in tracer.steps():
            fh.write(json.dumps(step, sort_keys=True, separators=(",", ":"),
                                default=str) + "\n")
```

- [ ] **Step 4: Write `run.py`**

```python
#!/usr/bin/env python3
"""Pipeline entry point (design doc §3).

This module and everything it imports have NO code path that reads ground
truth (invariant #5). Evaluation lives in a separate entry point that invokes
this one as a black box.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from obs_pipeline.bundle import file_hash, make_manifest, write_bundle
from obs_pipeline.claims import build_claims
from obs_pipeline.confidence import entity_confidence, stability
from obs_pipeline.entity import resolve_entities
from obs_pipeline.extract import load_observations
from obs_pipeline.fields import observation_fields, resolve_fields
from obs_pipeline.loader import load_rules
from obs_pipeline.trace import Tracer


def _engine_commit() -> str:
    """§6.2 -- rules alone do not determine behaviour; the engine interprets
    them. A clean HEAD sha reported while UNCOMMITTED code actually ran is
    worse than no value at all: it looks precise and is silently wrong. The
    `-dirty` suffix is what makes this honest rather than decorative.
    """
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                             text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"],
                               capture_output=True, text=True,
                               check=True).stdout.strip()
        return f"git:{sha}-dirty" if dirty else f"git:{sha}"
    except Exception:
        return "git:unknown"


def _run_id(input_hash: str, out_root) -> str:
    """Second-precision timestamp plus an input fingerprint.

    Two runs inside one second would otherwise share a directory and the
    later would silently clobber the earlier. When input and rules are
    unchanged the outputs are identical and overwriting is harmless, but
    run_id does not capture the ENGINE, so an edit-and-rerun inside one
    second is real data loss. A suffix costs nothing and never clobbers.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    base = f"{stamp}-{input_hash.split(':')[1][:6]}"
    candidate, n = base, 1
    while (Path(out_root) / candidate).exists():
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def run_pipeline(observations_path, rules_dir, out_root) -> Path:
    tracer = Tracer()
    # load_rules writes its stale-bump state file here, so the directory must
    # exist before the first run — otherwise a fresh checkout crashes on
    # `python3 run.py`, which pytest hides because its fixture pre-creates it.
    Path(out_root).mkdir(parents=True, exist_ok=True)
    rules = load_rules(rules_dir, observations_path,
                       state_path=Path(out_root) / "last_rules_state.json")
    observations = load_observations(observations_path)

    claims = build_claims(observations, rules, tracer)
    memberships = resolve_entities(claims, observations, rules, tracer)
    resolved = resolve_fields(claims, memberships, rules, tracer)
    obs_fields = observation_fields(claims, memberships, resolved, rules, tracer)

    entity_steps, stability_steps = {}, {}
    for entity_id in sorted(resolved):
        entity_steps[entity_id] = entity_confidence(resolved[entity_id], tracer)
        stability_steps[entity_id] = stability(resolved[entity_id], rules, tracer)

    input_hash = file_hash(observations_path)
    run_id = _run_id(input_hash, out_root)
    manifest = make_manifest(rules, input_hash, run_id, _engine_commit())

    run_dir = Path(out_root) / run_id
    write_bundle(run_dir, manifest=manifest, claims=claims,
                 memberships=memberships, resolved=resolved,
                 obs_fields=obs_fields, entity_steps=entity_steps,
                 stability_steps=stability_steps, tracer=tracer)
    return run_dir


if __name__ == "__main__":
    out = run_pipeline(
        sys.argv[1] if len(sys.argv) > 1 else "obs-data/observations.csv",
        sys.argv[2] if len(sys.argv) > 2 else "rules",
        sys.argv[3] if len(sys.argv) > 3 else "runs",
    )
    print(out)
```

- [ ] **Step 5: Run the pipeline once by hand**

Run: `python3 run.py`
Expected: prints a path like `runs/2026-08-04T…-a3f9c1`

- [ ] **Step 6: Run test to verify it passes**

Run: `python3 -m pytest tests/test_bundle.py -v`
Expected: PASS, 12 tests

- [ ] **Step 7: Commit**

```bash
git add obs_pipeline/bundle.py run.py tests/test_bundle.py
git commit -m "feat: run bundle and pipeline entry point"
```

---

### Task 13: `replay.py` — the completeness test

**Files:**
- Create: `replay.py`
- Test: `tests/test_replay.py`

**Interfaces:**
- Consumes: `obs_pipeline.trace` and `obs_pipeline.bundle` **only**.
- Produces: `reconstruct(trace_path) -> dict[str, list[dict]]` keyed `"claims" | "membership" | "entities"`; `replay_diff(run_dir) -> list[str]` returning human-readable diff lines, empty when the trace fully explains the outputs.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_replay.py
import ast
import json
from pathlib import Path

import pytest

from replay import replay_diff
from run import run_pipeline


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    return run_pipeline("obs-data/observations.csv", "rules",
                        tmp_path_factory.mktemp("runs"))


def test_trace_alone_reconstructs_every_output(bundle):
    """§9.5: if they match, the trace is PROVABLY sufficient to explain every
    emitted value. This is the mechanical enforcement of invariant #7."""
    assert replay_diff(bundle) == []


def test_replay_detects_claim_step_aliasing(bundle, tmp_path):
    """A set-of-step-ids comparison cannot see aliasing: if several claims
    shared one derivation step, both sides of the diff reduce to the same set
    and the gate passes while the trace is genuinely short. Deleting one
    score step must therefore be caught by COUNT, not by set membership."""
    aliased = tmp_path / "aliased"
    aliased.mkdir()
    for p in bundle.iterdir():
        (aliased / p.name).write_bytes(p.read_bytes())
    lines = (aliased / "trace.jsonl").read_text().strip().splitlines()
    dropped, kept = None, []
    for line in lines:
        step = json.loads(line)
        if dropped is None and step["op"] == "score":
            dropped = step
            continue
        kept.append(line)
    (aliased / "trace.jsonl").write_text("\n".join(kept) + "\n")
    diff = replay_diff(aliased)
    assert any(line.startswith("claims:") for line in diff), diff


def test_replay_detects_a_hole_in_the_trace(bundle, tmp_path):
    """If any field cannot be reconstructed, the diff must name the hole."""
    broken = tmp_path / "broken"
    broken.mkdir()
    for p in bundle.iterdir():
        (broken / p.name).write_bytes(p.read_bytes())

    lines = (broken / "trace.jsonl").read_text().strip().splitlines()
    kept = [ln for ln in lines if json.loads(ln)["op"] != "resolve_entity"]
    (broken / "trace.jsonl").write_text("\n".join(kept) + "\n")

    diff = replay_diff(broken)
    assert diff
    assert any("entities" in line for line in diff)


def test_replay_does_not_import_the_engine():
    """§2.1 of the implementation spec: if replay could reach the engine it
    might reconstruct a value by RECOMPUTING it rather than by reading the
    trace, and the completeness test would pass on an incomplete trace."""
    tree = ast.parse(Path("replay.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    engine = {"obs_pipeline.scoring", "obs_pipeline.entity", "obs_pipeline.fields",
              "obs_pipeline.claims", "obs_pipeline.extract", "obs_pipeline.loader",
              "obs_pipeline.normalize", "obs_pipeline.confidence", "run"}
    assert not (imported & engine), f"replay.py reaches the engine: {imported & engine}"


def test_replay_reads_no_file_other_than_the_trace(bundle, tmp_path):
    """§9.5: replay.py reads trace.jsonl ALONE -- no observations.csv, no rules."""
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    for name in ["trace.jsonl", "claims.csv", "membership.csv", "entities.csv"]:
        (isolated / name).write_bytes((bundle / name).read_bytes())
    assert replay_diff(isolated) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_replay.py -v`
Expected: FAIL — `No module named 'replay'`

- [ ] **Step 3: Write the implementation**

```python
#!/usr/bin/env python3
"""Replay: the trace completeness test (design doc §9.5).

Reads trace.jsonl ALONE -- no observations.csv, no rules -- and reconstructs
claims.csv, membership.csv and entities.csv. The reconstruction is diffed
against the actual run outputs. If they match, the trace is provably
sufficient to explain every emitted value.

This module must NEVER import the engine. Reaching scoring/entity/fields would
let it recompute a value instead of reading it, and the guarantee would be
vacuous. Enforced by tests/test_replay.py.
"""
from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path

FIELDS = ["vendor", "model", "device_type", "firmware"]


def _obs_of(step, by_id) -> str | None:
    """Recover which observation a step derives from, by walking parents to an
    `obs:` input. Only possible because score steps name their evidence."""
    frontier, seen = list(step["parents"]), set()
    while frontier:
        sid = frontier.pop()
        if sid in seen:
            continue
        seen.add(sid)
        parent = by_id.get(sid)
        if parent is None:
            continue
        for ref in parent["inputs"]:
            if ref.startswith("obs:"):
                return ref.split(":", 1)[1].split("#", 1)[0]
        frontier += parent["parents"]
    return None


def _load_trace(path) -> list[dict]:
    return [json.loads(line) for line in
            Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def reconstruct(trace_path) -> dict[str, list[dict]]:
    steps = _load_trace(trace_path)
    by_id = {s["step_id"]: s for s in steps}

    claims = []
    for s in steps:
        if s["op"] != "score":
            continue
        d = s["decomposition"]
        claims.append({
            "derivation_step": s["step_id"],
            "obs_id": _obs_of(s, by_id),
            "key": s["key"],
            "value": s["value"],
            "weight": s["output"],
            "witness_groups": "|".join(d["witness_groups"]),
        })

    membership = []
    for s in steps:
        if s["op"] != "assign_entity":
            continue
        membership.append({
            "derivation_step": s["step_id"],
            "obs_id": s["inputs"][0].split(":", 1)[1],
            "entity_id": s["output"],
            "link_basis": s["detail"]["link_basis"] or "none",
            "basis_agreement": str(s["detail"]["basis_agreement"]),
        })

    field_by_entity: dict[str, dict[str, dict]] = {}
    for s in steps:
        if s["op"] != "resolve_field":
            continue
        entity_id = s["inputs"][0].split(":", 1)[1]
        field_by_entity.setdefault(entity_id, {})[s["field"]] = {
            "value": s["output"], "confidence": s["confidence"],
        }

    stability_by_entity = {}
    for s in steps:
        if s["op"] == "stability":
            for parent in s["parents"]:
                p = by_id.get(parent)
                if p and p["op"] == "resolve_field":
                    stability_by_entity[p["inputs"][0].split(":", 1)[1]] = s["output"]

    entities = []
    for s in steps:
        if s["op"] != "resolve_entity":
            continue
        entity_id = None
        for parent in s["parents"]:
            p = by_id.get(parent)
            if p and p["op"] == "resolve_field":
                entity_id = p["inputs"][0].split(":", 1)[1]
                break
        if entity_id is None:
            continue
        row = {"derivation_step": s["step_id"], "entity_id": entity_id,
               "confidence": s["output"],
               "stability": stability_by_entity.get(entity_id)}
        for f in FIELDS:
            got = field_by_entity.get(entity_id, {}).get(f, {})
            row[f] = got.get("value", "")
            row[f"{f}_confidence"] = got.get("confidence")
        entities.append(row)

    return {"claims": claims, "membership": membership, "entities": entities}


def _read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def replay_diff(run_dir) -> list[str]:
    run_dir = Path(run_dir)
    rebuilt = reconstruct(run_dir / "trace.jsonl")
    problems: list[str] = []

    # Compare per-(obs_id, key, value) MULTISETS, not a set of step ids.
    # A set comparison cannot see aliasing: if N claims collapsed onto one
    # shared step id, both sides reduce to the same set and the diff reports
    # nothing while the trace is genuinely short by N-1 derivations.
    actual_claims = Counter(
        (r["obs_id"], r["key"], r["value"])
        for r in _read_csv(run_dir / "claims.csv")
    )
    rebuilt_claims = Counter(
        (r["obs_id"], r["key"], r["value"]) for r in rebuilt["claims"]
    )
    for signature in sorted(set(actual_claims) | set(rebuilt_claims)):
        want, got = actual_claims[signature], rebuilt_claims[signature]
        if want != got:
            problems.append(
                f"claims: {signature} appears {want}x in output, {got}x in trace"
            )

    actual_mem = {r["obs_id"]: r for r in _read_csv(run_dir / "membership.csv")}
    rebuilt_mem = {r["obs_id"]: r for r in rebuilt["membership"]}
    for obs_id, row in sorted(actual_mem.items()):
        got = rebuilt_mem.get(obs_id)
        if got is None:
            problems.append(f"membership: {obs_id} not reconstructible from trace")
        elif got["entity_id"] != row["entity_id"]:
            problems.append(
                f"membership: {obs_id} entity {row['entity_id']} != {got['entity_id']}"
            )

    actual_ent = {r["entity_id"]: r for r in _read_csv(run_dir / "entities.csv")}
    rebuilt_ent = {r["entity_id"]: r for r in rebuilt["entities"]}
    for entity_id, row in sorted(actual_ent.items()):
        got = rebuilt_ent.get(entity_id)
        if got is None:
            problems.append(f"entities: {entity_id} not reconstructible from trace")
            continue
        for f in FIELDS:
            if str(got.get(f, "")) != row[f]:
                problems.append(
                    f"entities: {entity_id}.{f} '{row[f]}' != '{got.get(f)}'"
                )
        if str(got["confidence"]) != row["confidence"]:
            problems.append(
                f"entities: {entity_id}.confidence "
                f"{row['confidence']} != {got['confidence']}"
            )
    return problems


if __name__ == "__main__":
    diff = replay_diff(sys.argv[1])
    for line in diff:
        print(line)
    print(f"{'REPLAY OK' if not diff else f'REPLAY FAILED: {len(diff)} holes'}")
    sys.exit(1 if diff else 0)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_replay.py -v`
Expected: PASS, 4 tests

If `test_trace_alone_reconstructs_every_output` fails, the fix is to emit the missing information **in the trace step**, never to weaken the diff. The diff naming a hole is the feature.

- [ ] **Step 5: Commit**

```bash
git add replay.py tests/test_replay.py
git commit -m "feat: replay as the mechanical trace-completeness gate"
```

---

### Task 14: Named regression cases and determinism

**Files:**
- Test: `tests/test_regression_cases.py`, `tests/test_determinism.py`

**Interfaces:**
- Consumes: `run.run_pipeline`, `replay.replay_diff`.
- Produces: nothing importable — these are gates.

- [ ] **Step 1: Write the regression-case tests**

```python
# tests/test_regression_cases.py
"""The five named cases from design doc §7.4, plus the false-merge trap.

These are asserted INDIVIDUALLY, not as a rate. §7.4: ground truth is 68
entities over 74 observations with only ~5 positive pairs, and any threshold
'met' at that n is noise. The small multi-observation set is still valuable --
as named regression cases that must each resolve correctly in isolation.
"""
import csv

import pytest

from run import run_pipeline


@pytest.fixture(scope="module")
def resolutions(tmp_path_factory):
    d = run_pipeline("obs-data/observations.csv", "rules",
                     tmp_path_factory.mktemp("runs"))
    with open(d / "resolutions.csv", newline="", encoding="utf-8") as fh:
        return {r["obs_id"]: r for r in csv.DictReader(fh)}


def _same_entity(res, *obs_ids):
    return len({res[o]["entity_id"] for o in obs_ids}) == 1


def test_e001_three_sources_one_mac(resolutions):
    assert _same_entity(resolutions, "OBS-001", "OBS-002", "OBS-003")
    assert resolutions["OBS-001"]["vendor"] == "Axis Communications"
    assert resolutions["OBS-001"]["model"] == "P3245-LVE"


def test_e002_two_sources_one_mac_differing_hostnames(resolutions):
    assert _same_entity(resolutions, "OBS-004", "OBS-005")


def test_e052_mac_beats_hostname_token(resolutions):
    """Same MAC, different hostname AND different IP."""
    assert _same_entity(resolutions, "OBS-047", "OBS-072")


def test_e066_links_by_serial_when_mac_is_empty(resolutions):
    assert _same_entity(resolutions, "OBS-061", "OBS-073")


def test_e066_propagates_vendor_to_the_evidence_poor_member(resolutions):
    """§3.1: OBS-073 contributes almost nothing -- whatever vendor it shows
    was carried in from OBS-061. Diffing that row naively against labels would
    credit the pipeline for extraction it never performed."""
    assert resolutions["OBS-073"]["vendor"] == "Hikvision"


def test_e074_firmware_conflict_is_undecidable(resolutions):
    assert _same_entity(resolutions, "OBS-069", "OBS-074")
    assert resolutions["OBS-069"]["firmware"] == "undecidable"
    assert resolutions["OBS-074"]["firmware"] == "undecidable"


def test_no_false_merge_across_identical_model_and_vendor(resolutions):
    """OBS-045..052 are eight distinct Axis P3245-LVE cameras. §7.3 Stage 3
    biases toward precision because a false merge corrupts every member's
    fields via propagation in Stage 4."""
    block = [f"OBS-{n:03d}" for n in range(45, 53)]
    entities = {resolutions[o]["entity_id"] for o in block}
    assert len(entities) == 8


def test_hikvision_block_stays_six_distinct_devices(resolutions):
    """OBS-059..064 share a model realm and differ only by X-Serial."""
    block = [f"OBS-{n:03d}" for n in range(59, 65)]
    assert len({resolutions[o]["entity_id"] for o in block}) == 6
```

- [ ] **Step 2: Write the determinism tests**

```python
# tests/test_determinism.py
"""Design doc §1 and §9.2: identical input and rules produce identical output,
and content-addressed step_ids make traces diffable across rule versions. A
sequence counter would make every trace superficially different and destroy
that property."""
import json

import pytest

from replay import replay_diff
from run import run_pipeline


@pytest.fixture(scope="module")
def two_runs(tmp_path_factory):
    root = tmp_path_factory.mktemp("runs")
    return (run_pipeline("obs-data/observations.csv", "rules", root),
            run_pipeline("obs-data/observations.csv", "rules", root))


def test_trace_is_byte_identical_across_runs(two_runs):
    a, b = two_runs
    assert (a / "trace.jsonl").read_bytes() == (b / "trace.jsonl").read_bytes()


def test_entity_ids_are_identical_across_runs(two_runs):
    a, b = two_runs
    assert (a / "entities.csv").read_text() == (b / "entities.csv").read_text()


def test_only_run_id_and_engine_commit_vary_between_manifests(two_runs):
    a, b = two_runs
    ma = json.loads((a / "manifest.json").read_text())
    mb = json.loads((b / "manifest.json").read_text())
    for key in ["input_hash", "rules_rollup", "rules_version", "rules_files"]:
        assert ma[key] == mb[key]


def test_replay_passes_on_a_fresh_run(two_runs):
    assert replay_diff(two_runs[0]) == []
```

- [ ] **Step 3: Run both suites**

Run: `python3 -m pytest tests/test_regression_cases.py tests/test_determinism.py -v`
Expected: PASS, 12 tests

`test_trace_is_byte_identical_across_runs` will catch any residual nondeterminism — set iteration leaking into a step body, a timestamp in a decision, a dict ordering dependency. If it fails, the offending value is in the first differing line of the two traces:

```bash
diff <(head -200 "$RUN_A/trace.jsonl") <(head -200 "$RUN_B/trace.jsonl") | head -5
```

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest -v`
Expected: PASS, all tests across 13 files

- [ ] **Step 5: Commit**

```bash
git add tests/test_regression_cases.py tests/test_determinism.py
git commit -m "test: named regression cases, false-merge trap, determinism"
```

---

### Task 15: `report.py` — the derived render

**Files:**
- Create: `obs_pipeline/report.py`
- Modify: `run.py` — call `write_report` at the end of `run_pipeline`
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: the written bundle only — reads the CSVs back, makes no decisions.
- Produces: `write_report(run_dir) -> Path`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_report.py
import pytest

from obs_pipeline.report import write_report
from run import run_pipeline


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    return run_pipeline("obs-data/observations.csv", "rules",
                        tmp_path_factory.mktemp("runs"))


def test_report_is_written(bundle):
    assert (bundle / "REPORT.md").exists()


def test_report_is_regenerable_from_the_bundle_alone(bundle):
    """§3: REPORT.md is a build artifact derived from the structured files. If
    it is lost it can be regenerated; nothing downstream may depend on parsing
    it."""
    before = (bundle / "REPORT.md").read_text()
    (bundle / "REPORT.md").unlink()
    write_report(bundle)
    assert (bundle / "REPORT.md").read_text() == before


def test_report_names_the_rule_state_it_was_produced_under(bundle):
    text = (bundle / "REPORT.md").read_text()
    assert "0.1.0" in text
    assert "sha256:" in text


def test_report_surfaces_undecidable_and_unknown_counts(bundle):
    text = (bundle / "REPORT.md").read_text().lower()
    assert "undecidable" in text
    assert "unknown" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_report.py -v`
Expected: FAIL — `No module named 'obs_pipeline.report'`

- [ ] **Step 3: Write the implementation**

```python
# obs_pipeline/report.py
"""REPORT.md -- a derived render, never a source of truth (design doc §3).

This inverts the usual arrangement deliberately: anything worth trending
should be queryable without re-parsing prose. This module reads the bundle's
structured files and makes no decisions of its own, so it emits no trace steps.
"""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

FIELDS = ["vendor", "model", "device_type", "firmware"]


def _read(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_report(run_dir) -> Path:
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    entities = _read(run_dir / "entities.csv")
    membership = _read(run_dir / "membership.csv")
    claims = _read(run_dir / "claims.csv")

    sizes = Counter(m["entity_id"] for m in membership)
    singletons = sum(1 for c in sizes.values() if c == 1)
    contested = sum(1 for m in membership if m["basis_agreement"] == "False")

    lines = [
        "# Run Report",
        "",
        "*Generated from the run bundle. Never hand-edit — regenerate with "
        "`python3 -c \"from obs_pipeline.report import write_report; "
        f"write_report('{run_dir}')\"`.*",
        "",
        "## Run provenance",
        "",
        f"- `run_id`: `{manifest['run_id']}`",
        f"- `rules_version`: **{manifest['rules_version']}**",
        f"- `rules_rollup`: `{manifest['rules_rollup']}`",
        f"- `input_hash`: `{manifest['input_hash']}`",
        f"- `version_verified`: **{manifest['version_verified']}**",
        f"- `engine_commit`: `{manifest['engine_commit']}`",
        "",
        "## Clustering shape",
        "",
        f"- Observations: **{len(membership)}**",
        f"- Entities: **{len(entities)}**",
        f"- Singletons: **{singletons}** "
        f"({singletons / max(len(entities), 1):.0%})",
        f"- Largest cluster: **{max(sizes.values()) if sizes else 0}**",
        f"- Observations with cross-basis contradiction: **{contested}**",
        "",
        "## Field resolution",
        "",
        "| Field | Known | Unknown | Undecidable | Mean confidence |",
        "|---|---|---|---|---|",
    ]

    for f in FIELDS:
        values = [e[f] for e in entities]
        unknown = sum(1 for v in values if v in ("Unknown", "unknown", ""))
        undecidable = sum(1 for v in values if v == "undecidable")
        known = len(values) - unknown - undecidable
        confs = [float(e[f"{f}_confidence"]) for e in entities]
        mean = sum(confs) / len(confs) if confs else 0.0
        lines.append(f"| `{f}` | {known} | {unknown} | {undecidable} | {mean:.2f} |")

    rejected = [c["value"] for c in claims if c["in_vocab"] == "False"]
    lines += [
        "",
        "## Vocabulary rejects",
        "",
        "*Ranked expansion queue (§6.3). A frequently-rejected value is either "
        "a missing vocabulary entry or a missing alias.*",
        "",
    ]
    if rejected:
        lines += ["| Value | Count |", "|---|---|"]
        lines += [f"| `{v}` | {n} |" for v, n in Counter(rejected).most_common()]
    else:
        lines.append("*None.*")

    out = run_dir / "REPORT.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
```

- [ ] **Step 4: Wire it into `run.py`**

In `run.py`, add the import and call it just before the `return`:

```python
from obs_pipeline.report import write_report
```

```python
    write_report(run_dir)
    return run_dir
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_report.py -v`
Expected: PASS, 4 tests

- [ ] **Step 6: Run the full suite and inspect the report**

Run:
```bash
python3 -m pytest -q
python3 run.py
cat "$(ls -dt runs/*/ | head -1)/REPORT.md"
```
Expected: all tests pass; the report shows 74 observations, roughly 68 entities, and a vocabulary-reject table containing `LTS Security`, `Amcrest`, `Wisenet` and `VVTK`.

- [ ] **Step 7: Commit**

```bash
git add obs_pipeline/report.py run.py tests/test_report.py
git commit -m "feat: REPORT.md as a pure render over the bundle"
```

---

## Done criteria for this plan

- [ ] `python3 -m pytest -q` passes with no failures
- [ ] `python3 run.py && python3 replay.py "$(ls -dt runs/*/ | head -1)"` exits 0 and prints `REPLAY OK`
- [ ] Two consecutive runs produce byte-identical `trace.jsonl`
- [ ] All five named §7.4 cases pass individually, plus the false-merge trap
- [ ] `REPORT.md` lists the four known alias gaps as vocabulary rejects

Phases 4 and 5 (metrics registry, label import, eval harness, four-bucket diff, blind adjudication) are covered by `2026-08-04-pipeline-eval.md`.
