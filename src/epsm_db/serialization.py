"""Portable evidence records, preserving dates and raw observations without credentials."""

from dataclasses import fields
from datetime import date, datetime
from enum import Enum
from uuid import UUID

from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry


def _encode(value):
    if isinstance(value, BaseGeometry):
        return {"__epsm_geometry__": "EPSG:3006", "value": mapping(value)}
    if isinstance(value, (datetime, date)):
        return {"__epsm_date__": type(value).__name__, "value": value.isoformat()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _encode(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_encode(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"Unsupported evidence value {type(value).__name__}")


def _decode(value):
    if isinstance(value, dict):
        if set(value) == {"__epsm_geometry__", "value"}:
            if value["__epsm_geometry__"] != "EPSG:3006":
                raise ValueError("Evidence geometries must use EPSG:3006")
            return shape(value["value"])
        if set(value) == {"__epsm_date__", "value"}:
            cls = datetime if value["__epsm_date__"] == "datetime" else date
            return cls.fromisoformat(value["value"])
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


def record_to_dict(record):
    data = {
        f.name: _encode(getattr(record, f.name))
        for f in fields(record)
        if not f.name.startswith("_") and f.name not in {"geometry", "receipt"}
    }
    data["geometry"] = mapping(record.geometry) if record.geometry is not None else None
    data["receipt"] = _encode(record.receipt.to_dict())
    return {"format": "epsm-record-1", "crs": "EPSG:3006", "record": data}


def record_from_dict(value):
    from sweden_ubem.enums import BuildingType
    from sweden_ubem.records import BuildingRecord, Receipt

    if value.get("format") != "epsm-record-1" or value.get("crs") != "EPSG:3006":
        raise ValueError("Expected EPSM record format 1 in EPSG:3006")
    data = _decode(value["record"])
    if any(k.startswith("_") for k in data):
        raise ValueError("Portable records must not contain connection/runtime fields")
    data["geometry"] = shape(data["geometry"]) if data["geometry"] else None
    data["type"] = BuildingType(data["type"])
    data["receipt"] = Receipt(**data["receipt"])
    for key in (
        "components",
        "heights",
        "purposes",
        "addresses",
        "properties",
        "certificates",
        "observations",
        "subjects",
        "epc_results",
        "quality_findings",
        "archetype_assignments",
    ):
        if key in data:
            data[key] = tuple(data[key])
    return BuildingRecord(**data)
