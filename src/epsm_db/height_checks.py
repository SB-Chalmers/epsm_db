"""Plausibility checks on the LiDAR body heights of small houses (småhus).

The height extractor takes unclassified LiDAR points inside a footprint as roof
points. Unclassified points include vegetation, so a house built after its area
was scanned, on a plot that was wooded at the time, receives the tree-canopy
height. Such heights pass the extractor's own checks (only bare plots fail as
``implausibly_low_height``), and storey inference then turns a bungalow into a
tower. In the 2026-10-13 national snapshot, 23-35 % of småhus built 2022-2026
have a body height of 7.5 m or more, against about 1.5 % of older houses.

These checks only report findings. They never change or drop evidence.
"""

from __future__ import annotations

import math

from .enums import BuildingType

#: Body height above which a detached small house would need three or more
#: full storeys at the typical floor height. Real småhus rarely exceed this.
SMALL_HOUSE_MAX_BODY_HEIGHT_M = 7.5
#: Floor-to-floor height used to turn a body height into a storey count.
TYPICAL_FLOOR_HEIGHT_M = 3.0
#: Lowest plausible register living area / (footprint x height-implied storeys).
#: Gross footprint to net living area is about 0.85-0.9 for one true storey, so
#: a ratio below 0.6 means the height implies storeys the house does not have.
MIN_LIVING_AREA_RATIO = 0.6
#: Minimum ridge-eave rise for a pitched roof, matching EPSM's record policy.
MIN_PITCHED_RISE_M = 0.1

_LIVING_AREA_KEYS = ("Byggnad/Bostad/totalBoarea", "Bostad/totalBoarea")


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def body_height(height):
    """Wall (body) height of one height-evidence row, or None.

    The eave for a supported pitched roof, otherwise the representative height,
    the same choice EPSM makes before inferring storeys.
    """
    if height.get("height_status") not in (None, "determined"):
        return None
    eave = height.get("eave_height_m")
    ridge = height.get("ridge_height_m")
    if (
        height.get("roof_form") == "pitched_ridge"
        and _positive(eave)
        and eave >= 2.0
        and (not _positive(ridge) or ridge - eave >= MIN_PITCHED_RISE_M)
    ):
        return float(eave)
    value = height.get("representative_height_m")
    return float(value) if _positive(value) else None


def is_small_house(record):
    """True for single-family records or any register purpose spelled Småhus."""
    if record.type == BuildingType.RESIDENTIAL_SINGLE_FAMILY:
        return True
    return any(
        str((purpose or {}).get("detail") or "").startswith("Småhus")
        for purpose in record.purposes or record.facts.get("purposes") or ()
    )


def register_living_area(record):
    """Lantmäteriet register total living area (totalBoarea) in m2, or None."""
    sources = [((record.facts.get("attributes") or {}).get("original_values") or {})]
    sources += [
        (purpose or {}).get("original_values") or {}
        for purpose in record.purposes or record.facts.get("purposes") or ()
    ]
    for values in sources:
        for key in _LIVING_AREA_KEYS:
            raw = values.get(key)
            if isinstance(raw, list):
                raw = raw[0] if raw else None
            try:
                area = float(raw)
            except (TypeError, ValueError):
                continue
            if area > 0:
                return area
    return None


def _year(value):
    if value is None:
        return None
    year = getattr(value, "year", None)
    if year is not None:
        return int(year)
    text = str(value)
    return int(text[:4]) if len(text) >= 4 and text[:4].isdigit() else None


def height_findings(record):
    """Findings about implausible small-house heights, as dicts with a ``code``.

    Codes:

    ``height_survey_before_construction``
        The height evidence was measured before the register construction year
        (time-period mismatch). Needs ``measured_at`` on the height evidence.
    ``small_house_height_implausible``
        Body height of a small-house component is at least
        ``SMALL_HOUSE_MAX_BODY_HEIGHT_M``.
    ``small_house_height_exceeds_living_area``
        Footprint x height-implied storeys is far larger than the register
        living area (ratio below ``MIN_LIVING_AREA_RATIO``) with 2+ storeys.
    """
    if not is_small_house(record):
        return []
    findings = []
    built = _year(record.facts.get("construction_year"))
    by_component = {}
    for height in record.heights or ():
        by_component.setdefault(height.get("component_id"), height)
        measured = _year(height.get("measured_at"))
        if built and measured and measured < built:
            findings.append(
                {
                    "code": "height_survey_before_construction",
                    "component_id": height.get("component_id"),
                    "detail": f"height measured {measured}, building constructed {built}",
                }
            )
    implied_area = 0.0
    max_storeys = 0
    for component in record.components or ():
        height = by_component.get(component.get("component_id"))
        value = body_height(height) if height else None
        geometry = component.get("geometry")
        if value is None or geometry is None:
            continue
        if value >= SMALL_HOUSE_MAX_BODY_HEIGHT_M:
            findings.append(
                {
                    "code": "small_house_height_implausible",
                    "component_id": component.get("component_id"),
                    "detail": f"body height {value:.1f} m >= {SMALL_HOUSE_MAX_BODY_HEIGHT_M} m",
                }
            )
        storeys = max(1, math.floor(value / TYPICAL_FLOOR_HEIGHT_M + 0.5))
        max_storeys = max(max_storeys, storeys)
        implied_area += geometry.area * storeys
    living = register_living_area(record)
    if living and implied_area > 0 and max_storeys >= 2:
        ratio = living / implied_area
        if ratio < MIN_LIVING_AREA_RATIO:
            findings.append(
                {
                    "code": "small_house_height_exceeds_living_area",
                    "component_id": None,
                    "detail": (
                        f"register living area {living:.0f} m2 is {ratio:.2f} of "
                        f"footprint x height-implied storeys ({implied_area:.0f} m2)"
                    ),
                }
            )
    return findings


def height_plausibility_field(record, findings):
    """Receipt field summarising :func:`height_findings`."""
    if not is_small_house(record):
        state = "not_assessed"
    elif not record.heights:
        state = "no_height_evidence"
    else:
        state = "implausible" if findings else "plausible"
    return {
        "source_state": state,
        "value": sorted({f["code"] for f in findings}),
        "origin": "small-house body height vs storey bounds, register living area and survey date",
    }
