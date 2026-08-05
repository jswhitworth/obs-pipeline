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
