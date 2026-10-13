"""Bounded EPC reads and conservative normalization of flattened source rows."""

from __future__ import annotations
import codecs
import csv
import math
from collections import defaultdict
from pathlib import Path
from .hashing import content_hash
from .linkage import normalize_designation


def detect_encoding(path: Path):
    with path.open("rb") as stream:
        prefix = stream.read(4)
    if prefix.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    if prefix.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    return "latin-1"


def strict_value(value, kind):
    raw = str(value).strip()
    if kind == "text":
        return raw
    if kind == "boolean":
        if raw.casefold() not in {"true", "false", "1", "0", "ja", "nej"}:
            raise ValueError("Invalid boolean")
        return raw.casefold() in {"true", "1", "ja"}
    if kind == "date":
        from datetime import datetime

        # ExpertGodkannande contains a date or an ISO-style date and time.
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    if kind == "integer":
        from decimal import Decimal, InvalidOperation

        try:
            integer = Decimal(raw.replace(" ", "").replace(",", "."))
        except InvalidOperation as e:
            raise ValueError("Invalid integer") from e
        if not integer.is_finite() or integer != integer.to_integral_value():
            raise ValueError("Non-integral integer")
        return int(integer)
    number = float(raw.replace(" ", "").replace(",", "."))
    if not math.isfinite(number):
        raise ValueError("Non-finite number")
    if kind == "number":
        return number
    raise ValueError("Unknown value type")


def iter_rows(root: Path):
    for path in sorted(root.glob("*/data/*.txt")):
        regime = path.parent.parent.name
        if regime not in ("fore_v260", "efter_v260"):
            continue
        with path.open(encoding=detect_encoding(path), newline="") as stream:
            reader = csv.DictReader(stream, delimiter="|")
            required = {"FormularId", "IdLankod", "IdKommunkod", "IdFastBet", "IdHusnr"}
            if not required <= set(reader.fieldnames or ()):
                raise ValueError(f"EPC subject schema mismatch: {path}")
            for ordinal, payload in enumerate(reader, 1):
                yield {
                    "source_file": str(path),
                    "source_record": str(ordinal),
                    "regime": regime,
                    "original_values": payload,
                }


def subject(row):
    p = row["original_values"]
    try:
        fid = strict_value(p["FormularId"], "integer")
        county = strict_value(p["IdLankod"], "integer")
        municipality = strict_value(p["IdKommunkod"], "integer")
        if not (0 < county < 100 and 0 <= municipality < 100):
            raise ValueError("Municipality parts")
        code = f"{county:02d}{municipality:02d}"
        designation = normalize_designation(p["IdFastBet"])
        if not designation:
            raise ValueError("Missing property designation")
        try:
            house = strict_value(p["IdHusnr"], "integer")
        except (ValueError, TypeError):
            house = None
    except (KeyError, ValueError, TypeError):
        return None
    cert = f"{row['regime']}:{fid}"
    identity = {
        "certificate": cert,
        "municipality": code,
        "designation": designation,
        "house": house,
    }
    return {
        "certificate_id": cert,
        "formular_id": fid,
        "epc_subject_id": content_hash(identity),
        "municipality_code": code,
        "designation_normalized": designation,
        "property_match_key": f"{code}|{designation}",
        "house_number": house,
        "original_values": p,
    }


def selected_rows(root: Path, municipality: str, certificate_ids=None):
    """Two bounded streaming passes when completing selected certificates.

    No national raw copy or in-memory national dataframe is created. The preview
    reports the full source bytes that must be streamed.
    """
    chosen = set(certificate_ids or ())
    if certificate_ids is None:
        for row in iter_rows(root):
            s = subject(row)
            if s and s["municipality_code"] == municipality:
                chosen.add(s["certificate_id"])
    for row in iter_rows(root):
        p = row["original_values"]
        try:
            key = f"{row['regime']}:{strict_value(p.get('FormularId'), 'integer')}"
        except (ValueError, TypeError):
            continue
        if key in chosen:
            yield row


def observations(rows, mappings=(), *, collect_locators=True):
    definitions = {(r["regime"], r["source_field"]): r for r in mappings}
    groups = defaultdict(lambda: {"values": set(), "locators": []})
    for row in rows:
        p = row["original_values"]
        cert = f"{row['regime']}:{strict_value(p['FormularId'], 'integer')}"
        for field, value in p.items():
            # Every field remains in raw rows. The query layer projects only the
            # agreed building-energy subset, avoiding irrelevant duplicated fields.
            if (row["regime"], field) not in definitions and not (
                field.startswith(("EgenAtemp", "Egi", "Vent"))
                or field
                in {"EgenNybyggAr", "EgenAntalPlan", "EgenAntalKallarplan", "ExpertGodkannande"}
            ):
                continue
            if value in (None, ""):
                continue
            group = groups[(cert, row["regime"], field)]
            group["values"].add(str(value))
            if collect_locators:
                group["locators"].append(
                    {"file": row["source_file"], "record": row["source_record"]}
                )
    result = []
    for (cert, regime, field), g in sorted(groups.items()):
        mapping = definitions.get((regime, field), {})
        verified = mapping.get("review_status") == "verified"
        owner = mapping.get("owner", "unverified")
        values = sorted(g["values"])
        value = None
        status = (
            "conflicting"
            if len(values) > 1 and verified and owner == "certificate"
            else "unverified"
        )
        # Child values require a verified child key, never a flattened row key.
        if len(values) == 1 and verified and owner == "certificate":
            try:
                value = strict_value(values[0], mapping["value_type"])
                if mapping["value_type"] == "number":
                    value *= float(mapping.get("multiplier", 1))
                status = "available"
            except (ValueError, TypeError):
                status = "failed"
        result.append(
            {
                "observation_id": content_hash([cert, field]),
                "certificate_id": cert,
                "owner_kind": owner,
                "owner_key": None,
                "source_field": field,
                "raw_values": values,
                "normalized_value": None if value is None else {"value": value},
                "source_unit": mapping.get("source_unit"),
                "normalized_unit": mapping.get("normalized_unit"),
                "value_status": status,
                "mapping_status": "verified" if verified else "unverified",
                "source_locators": g["locators"],
            }
        )
    return result
