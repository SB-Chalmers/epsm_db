"""Bounded rectilinear bay layouts for fixture evaluation, not a production default.

Internal bay cuts follow two building-aligned axes. Original outline/core contours
are retained; sizes are soft targets. Failure returns a reason without forcing geometry.
"""

from __future__ import annotations

import math
from itertools import pairwise

from shapely import set_precision
from shapely.affinity import rotate, translate
from shapely.geometry import box
from shapely.ops import unary_union

from examples.service_core_experiment import opened_area_fraction


def _pieces(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    return [p for g in getattr(geometry, "geoms", ()) for p in _pieces(g)]


def _slice_areas(polygon, areas):
    x0, y0, x1, y1 = polygon.bounds
    edges = [y0]
    target = 0.0
    for area in areas[:-1]:
        target += area
        lo, hi = edges[-1], y1
        for _ in range(40):
            mid = (lo + hi) / 2
            if polygon.intersection(box(x0 - 1, y0 - 1, x1 + 1, mid)).area < target:
                lo = mid
            else:
                hi = mid
        edges.append((lo + hi) / 2)
    edges.append(y1)
    return [polygon.intersection(box(x0 - 1, a, x1 + 1, b)) for a, b in pairwise(edges)]


def _side_units(side, lower_cap, upper_cap, target):
    lower = lower_cap.area > 1e-7
    upper = upper_cap.area > 1e-7
    end_areas = [
        (target - lower_cap.area) if lower else 0,
        (target - upper_cap.area) if upper else 0,
    ]
    if lower and end_areas[0] <= 0 or upper and end_areas[1] <= 0:
        return None
    remaining = side.area - sum(end_areas)
    if remaining <= 0:
        return None
    count = max(1, math.floor(remaining / target + 0.5))
    areas = (
        ([end_areas[0]] if lower else [])
        + [remaining / count] * count
        + ([end_areas[1]] if upper else [])
    )
    units = _slice_areas(side, areas)
    if lower:
        units[0] = units[0].union(lower_cap)
    if upper:
        units[-1] = units[-1].union(upper_cap)
    return units


def rectilinear_units(footprint, core, target=67.5, minimum=55, maximum=85):
    from epsm_core.roofs import dominant_direction

    last_reason = "no_usable_bay_layout"
    attempts = 0
    for offset in (0, 90):
        angle = dominant_direction(footprint) + offset
        oriented = rotate(footprint, -angle, origin=(0, 0))
        oriented_core = rotate(core, -angle, origin=(0, 0))
        ox, oy = oriented.bounds[:2]
        p = set_precision(translate(oriented, -ox, -oy), 1e-8)
        c = set_precision(translate(oriented_core, -ox, -oy), 1e-8)
        if p.geom_type != "Polygon" or c.geom_type != "Polygon":
            continue
        occupied = p.difference(c)
        x0, y0, x1, y1 = p.bounds
        a, b, d, e = c.bounds
        left = occupied.intersection(box(x0 - 1, y0 - 1, a, y1 + 1))
        right = occupied.intersection(box(d, y0 - 1, x1 + 1, y1 + 1))
        lower = occupied.intersection(box(a, y0 - 1, d, b))
        upper = occupied.intersection(box(a, e, d, y1 + 1))
        pockets = occupied.difference(unary_union([left, right, lower, upper]))
        if not left.area or not right.area:
            last_reason = "no_two_usable_facade_bays"
            continue
        # Four cap allocations and a narrow range of end-unit targets bound the search.
        for lower_side, upper_side in ((0, 0), (1, 1), (0, 1), (1, 0)):
            for unit_target in (target, target - 5, target + 5, target - 10, target + 10):
                attempts += 1
                empty = box(0, 0, 0, 0)
                left_units = _side_units(
                    left,
                    lower if lower_side == 0 else empty,
                    upper if upper_side == 0 else empty,
                    unit_target,
                )
                right_units = _side_units(
                    right,
                    lower if lower_side == 1 else empty,
                    upper if upper_side == 1 else empty,
                    unit_target,
                )
                if not left_units or not right_units:
                    last_reason = "cap_cannot_fit_unit_band"
                    continue
                # Clips can carry zero-area line remnants at notch boundaries.
                # Keep all polygonal area; these remnants are not thermal spaces.
                units = [unary_union(_pieces(u)) for u in left_units + right_units]
                if any(u.is_empty or u.geom_type not in ("Polygon", "MultiPolygon") for u in units):
                    last_reason = "empty_or_fragmented_bay"
                    continue
                failed = False
                # Core-contour pockets join an adjacent unit; none are discarded.
                for pocket in _pieces(pockets):
                    contacts = [
                        u.boundary.intersection(pocket.boundary).length
                        if u.boundary is not None
                        else 0
                        for u in units
                    ]
                    if max(contacts) < 1e-6:
                        failed = True
                        break
                    index = max(range(len(units)), key=lambda i: contacts[i])
                    units[index] = unary_union(_pieces(units[index].union(pocket)))
                if failed or any(
                    u.geom_type != "Polygon" or u.interiors or not u.is_valid for u in units
                ):
                    last_reason = "disconnected_or_holed_unit"
                    continue
                units = [set_precision(u, 1e-8) for u in units]
                if any(not minimum - 1e-6 <= u.area <= maximum + 1e-6 for u in units):
                    last_reason = "unit_area_outside_band"
                    continue
                metrics = [
                    {
                        "area_m2": u.area,
                        "width_retention": opened_area_fraction(u, 2.5),
                        "frontage_m": u.boundary.intersection(p.boundary).length,
                        "core_access_m": u.boundary.intersection(c.boundary).length,
                        "min_edge_m": min(
                            math.hypot(q[0] - r[0], q[1] - r[1])
                            for q, r in pairwise(u.exterior.coords)
                        ),
                    }
                    for u in units
                ]
                if any(m["width_retention"] < 0.95 for m in metrics):
                    last_reason = "narrow_unit"
                    continue
                if any(m["frontage_m"] < 1.2 or m["core_access_m"] < 0.9 for m in metrics):
                    last_reason = "insufficient_frontage_or_core_access"
                    continue
                if any(m["min_edge_m"] < 0.010001 for m in metrics):
                    last_reason = "subcentimetre_edge"
                    continue
                coverage = unary_union([c, *units])
                if coverage.symmetric_difference(p).area > p.area * 1e-6:
                    last_reason = "coverage_failure"
                    continue
                if abs(sum(u.area for u in units) + c.area - p.area) > p.area * 1e-6:
                    last_reason = "overlap_failure"
                    continue
                return {
                    "accepted": True,
                    "reason": "accepted",
                    "attempts": attempts,
                    "unit_count": len(units),
                    "units": metrics,
                    "aligned_angle_deg": angle,
                    "aligned_origin_m": [ox, oy],
                    "footprint_m2": p.area,
                    "core_m2": c.area,
                    "occupied_m2": sum(u.area for u in units),
                }, (p, c, tuple(units))
    return {"accepted": False, "reason": last_reason, "attempts": attempts}, None
