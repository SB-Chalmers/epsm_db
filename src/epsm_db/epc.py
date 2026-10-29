"""Reviewed EPC value parsing and bounded observation assembly; no database access."""

import codecs
import math
from collections import defaultdict


def detect_encoding(path):
    """UTF-16 or UTF-8 by byte-order mark; Swedish register exports default to Latin-1."""
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


def observations(rows, mappings=(), *, collect_locators=True):
    definitions = {(r["regime"], r["source_field"]): r for r in mappings}
    groups = defaultdict(lambda: {"values": set(), "locators": []})
    for row in rows:
        p = row["original_values"]
        cert = (
            row.get("certificate_id")
            or f"{row['regime']}:{strict_value(p['FormularId'], 'integer')}"
        )
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
                "observation_id": f"{cert}:{field}",
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
