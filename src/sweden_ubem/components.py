from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any, Iterable, Mapping


GEOMETRY_POLICY_VERSION = "lm-current-cohort-polygon-union-v3"


def normalized_geometry(geometry):
    """Tower-compatible normalized 2D geometry (CRS is intentionally external)."""
    import shapely
    from shapely.geometry import MultiPolygon

    geometry = shapely.force_2d(geometry)
    if isinstance(geometry, MultiPolygon) and len(geometry.geoms) == 1:
        geometry = geometry.geoms[0]
    return shapely.normalize(geometry)


def normalized_geometry_hash(geometry) -> str:
    import shapely

    if geometry is None or geometry.is_empty:
        raise ValueError("A component cannot have empty geometry")
    data = shapely.to_wkb(
        normalized_geometry(geometry), byte_order=1, output_dimension=2, include_srid=False
    )
    return hashlib.sha256(data).hexdigest()


def component_identifier(building_id: str, geometry) -> str:
    return f"{building_id}:{normalized_geometry_hash(geometry)}"


def assemble_building_snapshot(
    building_id: str, components: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    """Union the selected UUID cohort, accepting overlaps and disconnected parts.

    Representation labels are source evidence, not a manual-review gate. Component
    identities and geometries remain unchanged; the union counts overlap once.
    """
    import shapely

    rows = list(components)
    if not rows:
        raise ValueError(f"Building {building_id} has no components")
    if any(str(row["building_id"]) != building_id for row in rows):
        raise ValueError("Snapshot components must belong to one LM building UUID")
    geometries = [row["geometry"] for row in rows]
    invalid = any(
        g is None or g.is_empty or not g.is_valid
        or g.geom_type not in {"Polygon", "MultiPolygon"} or g.area <= 0
        for g in geometries
    )
    display = (
        shapely.GeometryCollection([g for g in geometries if g is not None])
        if invalid else shapely.union_all(geometries)
    )
    status = "invalid" if invalid else "resolved"
    flags = []
    if invalid:
        flags.append("invalid_component_geometry")
    elif display.geom_type == "MultiPolygon":
        flags.append("disconnected_parts_accepted")
    if not invalid and sum(g.area for g in geometries) - display.area > 1e-8:
        flags.append("component_overlap_accepted")
    return {
        "building_id": building_id,
        "component_count": len(rows),
        "area_m2": float(display.area),
        "resolution_status": status,
        "linkage_eligible": not invalid,
        "quality_flags": flags,
        "display_geometry": display,
        "linkage_geometry": display if not invalid else None,
    }


def relation_candidates(components: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Record overlap/containment as accepted union relationships, retaining evidence."""
    rows = sorted(components, key=lambda row: str(row["component_id"]))
    result: list[dict[str, Any]] = []
    for index, left in enumerate(rows):
        for right in rows[index + 1 :]:
            if left["building_id"] != right["building_id"]:
                continue
            intersection = left["geometry"].intersection(right["geometry"])
            overlap = float(intersection.area)
            if overlap <= 0:
                continue
            fraction = overlap / min(left["geometry"].area, right["geometry"].area)
            relation = "containment" if fraction >= 0.999999 else "overlap"
            result.append(
                {
                    "component_a": left["component_id"],
                    "component_b": right["component_id"],
                    "relation_type": relation,
                    "decision_status": "accepted_union",
                    "policy_version": GEOMETRY_POLICY_VERSION,
                    "overlap_area_m2": overlap,
                    "fraction_smaller": fraction,
                }
            )
    return result


def group_snapshots(components: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in components:
        grouped[str(row["building_id"])].append(row)
    return [assemble_building_snapshot(key, grouped[key]) for key in sorted(grouped)]
