from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping


POLICY_VERSION = "property-mediated-exact-house-50pct-v1"
MINIMUM_OVERLAP_FRACTION = 0.5


def normalize_designation(value: Any) -> str | None:
    if value is None:
        return None
    import unicodedata

    normalized = (
        " ".join(unicodedata.normalize("NFKC", str(value)).casefold().upper().split())
        .split(">", 1)[0]
        .strip()
    )
    return normalized or None


def property_match_key(municipality: Any, designation: Any) -> str | None:
    normalized = normalize_designation(designation)
    return f"{str(municipality).zfill(4)}|{normalized}" if normalized else None


def building_property_candidates(
    buildings: Iterable[Mapping[str, Any]], property_parts: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Index polygon parts, then union intersections by registered property identity.

    Candidate screening changes no acceptance rules. An index is needed now that
    valid UUID-cohort unions participate in source-selection expansion by default.
    """
    import shapely

    parts = list(property_parts)
    tree = shapely.STRtree([part["geometry"] for part in parts])
    result: list[dict[str, Any]] = []
    for building in buildings:
        geometry = building.get("linkage_geometry")
        if geometry is None or not building.get("linkage_eligible", False):
            continue
        area = float(geometry.area)
        candidates: list[dict[str, Any]] = []
        point = geometry.representative_point()
        hits_by_unit: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for index in tree.query(geometry, predicate="intersects"):
            part = parts[int(index)]
            hits_by_unit[str(part["property_unit_id"])].append(part)
        for unit_id, hits in hits_by_unit.items():
            intersection = float(
                shapely.union_all([geometry.intersection(part["geometry"]) for part in hits]).area
            )
            if intersection <= 0:
                continue
            candidates.append(
                {
                    "building_id": str(building["building_id"]),
                    "property_unit_id": unit_id,
                    "representative_point_within": any(
                        part["geometry"].covers(point) for part in hits
                    ),
                    "intersection_area_m2": intersection,
                    "overlap_fraction": intersection / area,
                    "positive_overlap": True,
                }
            )
        candidates.sort(
            key=lambda row: (
                -int(row["representative_point_within"]),
                -row["overlap_fraction"],
                row["property_unit_id"],
            )
        )
        primary = candidates[0]["overlap_fraction"] if candidates else None
        second = candidates[1]["overlap_fraction"] if len(candidates) > 1 else None
        for rank, row in enumerate(candidates):
            row.update(
                {
                    "is_primary_property": rank == 0,
                    "candidate_property_count": len(candidates),
                    "primary_overlap_fraction": primary,
                    "second_overlap_fraction": second,
                    "overlap_margin": None if primary is None else primary - (second or 0.0),
                }
            )
            result.append(row)
    return result


def derive_epc_candidates(
    buildings: Iterable[Mapping[str, Any]],
    property_candidates: Iterable[Mapping[str, Any]],
    property_designations: Iterable[Mapping[str, Any]],
    subjects: Iterable[Mapping[str, Any]],
    *,
    minimum_overlap_fraction: float = MINIMUM_OVERLAP_FRACTION,
) -> list[dict[str, Any]]:
    """Apply the pinned linkage policy and preserve accepted and rejected candidates."""
    buildings_by_id = {str(row["building_id"]): row for row in buildings}
    designations: dict[str, set[str]] = defaultdict(set)
    for row in property_designations:
        key = row.get("match_key") or property_match_key(
            row.get("municipality_code"),
            row.get("designation_normalized") or row.get("designation_raw"),
        )
        if key:
            designations[str(row["property_unit_id"])].add(str(key))
    subjects_by_key: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for subject in subjects:
        key = subject.get("property_match_key") or subject.get("property_key")
        if key:
            subjects_by_key[str(key)].append(subject)

    candidates_by_building: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for candidate in property_candidates:
        candidates_by_building[str(candidate["building_id"])].append(candidate)

    output: list[dict[str, Any]] = []
    for building_id, candidates in sorted(candidates_by_building.items()):
        building = buildings_by_id[building_id]
        eligible = [
            row for row in candidates if row["overlap_fraction"] >= minimum_overlap_fraction
        ]
        for candidate in candidates:
            unit_id = str(candidate["property_unit_id"])
            for match_key in sorted(designations.get(unit_id, ())):
                for subject in subjects_by_key.get(match_key, ()):
                    reasons: list[str] = []
                    if not building.get("linkage_eligible", False):
                        reasons.append("unresolved_footprint")
                    if len(eligible) != 1:
                        reasons.append("ambiguous_property_assignment")
                    if candidate["overlap_fraction"] < minimum_overlap_fraction:
                        reasons.append("insufficient_spatial_support")
                    left, right = subject.get("house_number"), building.get("house_number")
                    if left is None or right is None or str(left) != str(right):
                        reasons.append("house_number_disagreement")
                    output.append(
                        {
                            "building_id": building_id,
                            "epc_subject_id": str(subject["epc_subject_id"]),
                            "formular_id": int(subject["formular_id"]),
                            "certificate_id": subject.get(
                                "certificate_id", str(subject["formular_id"])
                            ),
                            "property_unit_id": unit_id,
                            "decision": "accepted" if not reasons else "rejected",
                            "evidence_tier": (
                                "A_exact_primary"
                                if not reasons and candidate.get("is_primary_property")
                                else "B_exact_boundary"
                                if not reasons
                                else None
                            ),
                            "rejection_reason": "|".join(reasons) or None,
                            "overlap_fraction": float(candidate["overlap_fraction"]),
                            "evidence": {
                                "policy_version": POLICY_VERSION,
                                "minimum_overlap_fraction": minimum_overlap_fraction,
                                "match_key": match_key,
                            },
                        }
                    )
    return apply_global_cardinalities(output)


def apply_global_cardinalities(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result = [dict(row) for row in rows]
    accepted = [row for row in result if row["decision"] == "accepted"]
    subject_buildings: dict[str, set[str]] = defaultdict(set)
    certificate_buildings: dict[int, set[str]] = defaultdict(set)
    building_certificates: dict[str, set[int]] = defaultdict(set)
    for row in accepted:
        subject_buildings[row["epc_subject_id"]].add(row["building_id"])
        certificate_buildings[row.get("certificate_id", row["formular_id"])].add(row["building_id"])
        building_certificates[row["building_id"]].add(row.get("certificate_id", row["formular_id"]))
    for row in result:
        row["subject_to_footprint_count"] = len(subject_buildings[row["epc_subject_id"]])
        row["certificate_to_footprint_count"] = len(
            certificate_buildings[row.get("certificate_id", row["formular_id"])]
        )
        row["building_to_certificate_count"] = len(building_certificates[row["building_id"]])
        row["cardinality_scope"] = "loaded_relations"
    return result
