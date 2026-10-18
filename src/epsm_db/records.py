"""Retrieved evidence and readable receipts; no EPSM imports or model defaults."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import pandas as pd

from .enums import BuildingType
from .epc import strict_value
from ._logging import operation as logged_operation


def _display_label(value, limit=24):
    """Bound a fully escaped label without cutting escape sequences."""
    text = str(value)
    if len(repr(text)) <= limit:
        return repr(text)
    text = text[:limit]
    while len(repr(text + "…")) > limit:
        text = text[:-1]
    return repr(text + "…")


def implementation_hash(module_name):
    """Pin executable package sources/resources, independent of install location."""
    root = Path(importlib.import_module(module_name).__file__).parent
    manifest = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.suffix in {".py", ".json"}
    }
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def json_default(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).hex()
    if hasattr(value, "wkb_hex"):
        return value.wkb_hex
    return str(value)


@dataclass
class Receipt:
    """Source availability and model completion are deliberately separate."""

    dataset_id: str
    building_id: str | None = None
    request: dict = field(default_factory=dict)
    snapshot: dict = field(default_factory=dict)
    fields: dict[str, dict] = field(default_factory=dict)
    quality: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    identities: dict = field(default_factory=dict)
    ready_for_simulation: bool = False
    truncated: bool = False
    next_cursor: str | None = None

    def __repr__(self):
        return (
            f"Receipt(dataset={_display_label(self.dataset_id)}, "
            f"building={_display_label(self.building_id)}, fields={len(self.fields)}, "
            f"errors={len(self.errors)}, ready={self.ready_for_simulation})"
        )

    __str__ = __repr__

    def summary(self):
        """Return independent loaded evidence and findings, without performing I/O."""
        return deepcopy(self.to_dict())

    @property
    def completeness(self):
        rows = [
            {
                "field": name,
                "source_state": data.get("source_state", "—"),
                "source_value": data.get("value"),
                "model_value": data.get("model_value"),
                "model_state": data.get("model_state", "—"),
            }
            for name, data in self.fields.items()
        ]
        return pd.DataFrame(rows).fillna("—")

    def to_dict(self):
        return {
            "dataset_id": self.dataset_id,
            "building_id": self.building_id,
            "request": self.request,
            "snapshot": self.snapshot,
            "fields": self.fields,
            "quality": self.quality,
            "assumptions": self.assumptions,
            "errors": self.errors,
            "identities": self.identities,
            "ready_for_simulation": self.ready_for_simulation,
            "truncated": self.truncated,
            "next_cursor": self.next_cursor,
        }

    def to_json(self):
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False, default=json_default)

    def report(self):
        """Format the loaded completeness table and findings without performing I/O."""
        title = f"{self.dataset_id}: {self.building_id or 'selection'}"
        lines = [title, f"Simulation ready: {self.ready_for_simulation}"]
        if self.fields:
            lines.append(self.completeness.to_string(index=False))
        for label, values in [
            ("Quality", self.quality),
            ("Assumptions", self.assumptions),
            ("Errors", self.errors),
        ]:
            lines.extend(f"{label}: {x}" for x in values)
        if self.truncated:
            lines.append(f"Selection truncated; continue after numeric cursor={self.next_cursor!r}")
        return "\n".join(lines)


@dataclass(frozen=True)
class BuildingRecord:
    dataset_id: str
    building_id: str
    type: BuildingType
    geometry: Any
    facts: dict
    components: tuple[dict, ...] = ()
    heights: tuple[dict, ...] = ()
    purposes: tuple[dict, ...] = ()
    addresses: tuple[dict, ...] = ()
    properties: tuple[dict, ...] = ()
    certificates: tuple[dict, ...] = ()
    observations: tuple[dict, ...] = ()
    subjects: tuple[dict, ...] = ()
    epc_results: tuple[dict, ...] = ()
    quality_findings: tuple[dict, ...] = ()
    receipt: Receipt = field(default_factory=lambda: Receipt("unknown"))
    archetype_assignments: tuple[dict, ...] = ()
    areas: tuple[dict, ...] = ()
    property_parts: tuple[dict, ...] = ()
    _database_url: str | None = field(default=None, repr=False, compare=False)

    def __repr__(self):
        return (
            f"BuildingRecord(id={_display_label(self.building_id)}, "
            f"dataset={_display_label(self.dataset_id)}, components={len(self.components)}, "
            f"observations={len(self.observations)})"
        )

    __str__ = __repr__

    def summary(self):
        """Inspect loaded evidence metadata without querying or exposing credentials."""
        return {
            "dataset_id": self.dataset_id,
            "building_id": self.building_id,
            "type": self.type.value,
            "facts": deepcopy(self.facts),
            "components": len(self.components),
            "observations": len(self.observations),
            "archetype_assignments": deepcopy(self.archetype_assignments),
            "receipt": self.receipt.summary(),
        }

    @property
    def crs(self):
        """Coordinate reference system of this database's metric geometries."""
        return "EPSG:3006"

    def to_dict(self):
        """Portable evidence without the database connection or credentials."""
        from epsm_db.serialization import record_to_dict

        return record_to_dict(self)

    @classmethod
    def from_dict(cls, value):
        from epsm_db.serialization import record_from_dict

        return record_from_dict(value)

    def completeness(self):
        """Field-level evidence availability retained by the retrieval receipt."""
        return self.receipt.completeness

    @property
    def selected_certificate(self):
        """Never break latest-date ties or hide certificates with unknown dates."""
        if not self.certificates:
            return None
        if any(
            c.get("certificate_date") is None or c.get("date_status") != "verified"
            for c in self.certificates
        ):
            return None
        latest = max(c["certificate_date"] for c in self.certificates)
        candidates = [c for c in self.certificates if c["certificate_date"] == latest]
        return candidates[0] if len(candidates) == 1 else None

    @property
    def temporal_warnings(self):
        selected = self.selected_certificate
        states = {
            "construction_conflict",
            "site_change_warning",
            "certificate_after_assessment_date",
            "construction_after_certificate",
            "temporal_conflict",
        }
        return tuple(
            r["temporal_status"]
            for r in self.epc_results
            if selected
            and r["certificate_id"] == selected["certificate_id"]
            and r.get("temporal_status") in states
        )

    @property
    def single_building_epc(self):
        c = self.selected_certificate
        return bool(
            c
            and c.get("subject_count") == 1
            and c.get("loaded_building_count") == 1
            and c.get("matching_status") == "fully_matched"
        )

    def observation(self, source_field, *, integer=False):
        c = self.selected_certificate
        rows = [
            o
            for o in self.observations
            if c
            and o["certificate_id"] == c["certificate_id"]
            and o["source_field"] == source_field
        ]
        if not rows:
            return None, "missing"
        if len(rows) != 1 or len(rows[0]["raw_values"]) != 1:
            return None, "conflicting"
        try:
            value = strict_value(rows[0]["raw_values"][0], "integer" if integer else "number")
        except (ValueError, TypeError):
            return None, "invalid"
        return value, "reported_verified_mapping" if rows[0].get(
            "mapping_status"
        ) == "verified" else "reported_unverified_mapping"

    @property
    def atemp_m2(self):
        return self.observation("EgenAtemp")[0] if self.single_building_epc else None

    @property
    def storeys(self):
        return (
            self.observation("EgenAntalPlan", integer=True)[0] if self.single_building_epc else None
        )

    @property
    def basement_storeys(self):
        return (
            self.observation("EgenAntalKallarplan", integer=True)[0]
            if self.single_building_epc
            else None
        )

    @property
    def programmes(self):
        fields = {
            "residential": "EgenAtempBostad",
            "office": "EgenAtempKontor",
            "school": "EgenAtempSkolor",
            "hotel": "EgenAtempHotell",
            "restaurant": "EgenAtempRestaurang",
            "grocery": "EgenAtempLivsmedel",
            "retail": "EgenAtempButik",
            "shopping": "EgenAtempKopcentrum",
            "healthcare": "EgenAtempVard",
            "daytime_service": "EgenAtempVardDag",
            "bath": "EgenAtempBad",
            "theatre": "EgenAtempTeater",
            "other": "EgenAtempOvrig",
        }
        result = {}
        if not self.single_building_epc:
            return result
        for name, source in fields.items():
            value, state = self.observation(source)
            if state in {"conflicting", "invalid"} or value is not None and not 0 <= value <= 100:
                raise ValueError(f"Invalid or conflicting programme share: {source}")
            if value:
                result[name] = value / 100
        if result and not math.isclose(sum(result.values()), 1, abs_tol=1e-7):
            raise ValueError("Reported programme shares do not sum to 100%; no normalization")
        return result

    @property
    def location(self):
        """A representative building point as latitude/longitude for weather."""
        from pyproj import Transformer

        if self.geometry is None or self.geometry.is_empty:
            raise ValueError("A building footprint is required for its weather location")
        point = self.geometry.representative_point()
        lon, lat = Transformer.from_crs(3006, 4326, always_xy=True).transform(point.x, point.y)
        return {"latitude": lat, "longitude": lon}

    def neighbours(self, *, radius_m=100, limit=1000):
        """Fetch a bounded neighbouring selection; portable records need a connection."""
        from .database import Database

        if not self._database_url:
            raise ValueError("Portable records have no database connection; use Database.stock().")
        if self.geometry is None:
            raise ValueError("A footprint is required for neighbours")
        return (
            Database(self._database_url)
            .stock()
            .within_radius(
                self.geometry,
                radius_m=radius_m,
                exclude=self.building_id,
            )
            .frame(limit=limit)
        )

    @logged_operation("database.record.convert", level=10)
    def to_epsm(self, **kwargs):
        from epsm import Building
        from ._logging import call_with_context

        return call_with_context(Building.from_record, self, **kwargs)


@dataclass(frozen=True)
class RecordCollection:
    records: tuple[BuildingRecord, ...]
    receipt: Receipt

    def __iter__(self):
        return iter(self.records)

    def __len__(self):
        return len(self.records)

    def __getitem__(self, item):
        return self.records[item]

    def one(self):
        from .errors import AmbiguousRecordError, RecordNotFoundError

        if not self.records:
            raise RecordNotFoundError(self.receipt)
        if len(self.records) != 1 or self.receipt.truncated:
            raise AmbiguousRecordError(self)
        return self.records[0]
