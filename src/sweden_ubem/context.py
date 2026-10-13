"""Projection and spatial-assignment logic for SCB contextual evidence."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import shapely

from .hashing import content_hash
from .scb import POPULATION_FIELDS, validate_scb_snapshot

CONTEXT_POLICY_VERSION = "scb-context-overlap-point-v1"


def _property(properties: Mapping[str, Any], *names: str) -> Any:
    folded = {str(key).casefold(): value for key, value in properties.items()}
    for name in names:
        value = folded.get(name.casefold())
        if value not in (None, ""):
            return value
    return None


def project_scb_snapshot(
    snapshot: Path,
    municipality_geometry,
    *,
    municipality: str = "1480",
    include_population: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Project the pinned Göteborg subset without mutating any source asset."""
    manifest = validate_scb_snapshot(snapshot)
    municipality = str(municipality).zfill(4)
    if manifest["municipality"] != municipality:
        raise ValueError("SCB snapshot municipality does not match dataset municipality")

    snapshot = Path(manifest["path"])
    regso_document = json.loads((snapshot / manifest["regso"]["response_file"]).read_text())
    areas: list[dict[str, Any]] = []
    raw_areas: list[dict[str, Any]] = []
    for source_fid, feature in enumerate(regso_document.get("features") or [], 1):
        properties = feature.get("properties") or {}
        code = str(_property(properties, "regsokod", "RegSO_kod", "regso_code", "REGSOKOD") or "")
        code = code.replace("_RegSO2025", "")
        if not code:
            raise ValueError("RegSO feature lacks an area code")
        geometry = shapely.geometry.shape(feature["geometry"])
        row = {
            "area_role": "regso_boundaries",
            "area_type": "regso",
            "area_code": code,
            "name": _property(properties, "regsonamn", "RegSO_namn", "regso_name"),
            "municipality_code": str(_property(properties, "kommunkod") or municipality).zfill(4),
            "reference_period": str(manifest["regso"]["year"]),
            "source_payload": properties,
            "geometry_wkb": shapely.to_wkb(geometry),
        }
        areas.append(row)
        raw_areas.append(
            {
                **row,
                "source_file": manifest["regso"]["response_file"],
                "source_member": None,
                "source_fid": source_fid,
                "original_values": properties,
            }
        )

    import pyogrio
    import pandas as pd

    from .bundle import translated_reference

    population_path = translated_reference(
        snapshot / "manifest.json", manifest["population_grid"]["path"],
        expected_sha256=manifest["population_grid"]["sha256"],
    )
    population = (
        pyogrio.read_dataframe(
            population_path,
            layer=manifest["population_grid"]["layer"],
            bbox=municipality_geometry.bounds,
        )
        if include_population
        else pd.DataFrame()
    )
    if include_population:
        population = population.loc[population.geometry.intersects(municipality_geometry)].copy()
        population.sort_values("objectid", inplace=True)
    raw_population: list[dict[str, Any]] = []
    indicators: dict[str, dict[str, Any]] = {}
    observations: list[dict[str, Any]] = []
    field_ids = {
        "beftotalt": "scb:population:total",
        "man": "scb:population:male",
        "kvinna": "scb:population:female",
        **{
            field: "scb:population:age_" + field.removeprefix("ald")
            for field in POPULATION_FIELDS
            if field.startswith("ald")
        },
    }
    for field, indicator_id in field_ids.items():
        indicators[indicator_id] = {
            "indicator_id": indicator_id,
            "publisher": "SCB",
            "dataset_code": "befolkning_1km_2025",
            "contents_code": field,
            "label": field,
            "source_unit": "person",
            "normalized_unit": "person",
            "description": "Published disclosure-protected SCB 1 km population-grid value",
            "mapping_version": "scb-population-grid-v1",
        }
    indicators["scb:population_density_1km"] = {
        "indicator_id": "scb:population_density_1km",
        "publisher": "SCB",
        "dataset_code": "befolkning_1km_2025",
        "contents_code": "beftotalt",
        "label": "Population density in containing 1 km grid cell",
        "source_unit": "person",
        "normalized_unit": "person/km2",
        "description": "Contextual density; not an estimate of building occupants",
        "mapping_version": "scb-population-grid-v1",
    }
    for _, source in population.iterrows():
        values = {name: source.get(name) for name in population.columns if name != "geometry"}
        code = str(source.rutid_scb)
        geometry = source.geometry
        area_row = {
            "area_role": "population_grid",
            "area_type": "population_grid_1km",
            "area_code": code,
            "name": None,
            "municipality_code": municipality,
            "reference_period": str(source.referenstid),
            "source_payload": values,
            "geometry_wkb": shapely.to_wkb(geometry),
        }
        areas.append(area_row)
        raw_population.append(
            {
                **area_row,
                "source_file": str(population_path),
                "source_member": manifest["population_grid"]["layer"],
                "source_fid": int(source.objectid),
                "grid_cell_id": code,
                "original_values": values,
            }
        )
        dimensions = {"grid_size_m": int(source.rutstorl)}
        for field, indicator_id in field_ids.items():
            value = source.get(field)
            missing = value is None or bool(pd.isna(value))
            numeric = None if missing else float(value)
            observations.append(
                _observation(
                    statistics_role="population_grid",
                    area_role="population_grid",
                    area_type="population_grid_1km",
                    area_code=code,
                    indicator_id=indicator_id,
                    reference_period=str(source.referenstid),
                    dimensions=dimensions,
                    raw_value=None if missing else str(value),
                    numeric_value=numeric,
                    source_unit="person",
                    normalized_value=numeric,
                    normalized_unit="person",
                    normalization_method="identity",
                    value_status="published_protected",
                    provenance={"source_fid": int(source.objectid), "source_field": field},
                )
            )
        total = source.get("beftotalt")
        total_missing = total is None or bool(pd.isna(total))
        size_km = float(source.rutstorl) / 1000.0
        density = None if total_missing or not size_km else float(total) / (size_km * size_km)
        observations.append(
            _observation(
                statistics_role="population_grid",
                area_role="population_grid",
                area_type="population_grid_1km",
                area_code=code,
                indicator_id="scb:population_density_1km",
                reference_period=str(source.referenstid),
                dimensions=dimensions,
                raw_value=None if total_missing else str(total),
                numeric_value=None if total_missing else float(total),
                source_unit="person",
                normalized_value=density,
                normalized_unit="person/km2",
                normalization_method="population / (grid_size_m / 1000)^2",
                value_status="published_protected",
                provenance={"source_fid": int(source.objectid), "source_field": "beftotalt"},
            )
        )

    income_document = json.loads((snapshot / manifest["income"]["response_file"]).read_text())
    income_request = json.loads((snapshot / manifest["income"]["request_file"]).read_text())
    columns = [str(column.get("code")) for column in income_document.get("columns") or []]
    indicators["scb:income:median_earned"] = {
        "indicator_id": "scb:income:median_earned",
        "publisher": "SCB",
        "dataset_code": manifest["income"]["dataset_code"],
        "contents_code": manifest["income"]["contents_code"],
        "label": "Median sammanräknad förvärvsinkomst",
        "source_unit": "tkr/year",
        "normalized_unit": "SEK/year",
        "description": "Median earned income for both sexes by RegSO",
        "mapping_version": "scb-pxweb-income-v1",
    }
    raw_values: list[dict[str, Any]] = []
    for source_row, item in enumerate(income_document.get("data") or [], 1):
        keys = [str(value) for value in item.get("key") or []]
        dimensions = dict(zip(columns, keys)) if columns else {"key": keys}
        region = str(dimensions.get("Region") or (keys[0] if keys else ""))
        region = region.replace("_RegSO2025", "")
        raw = str((item.get("values") or [""])[0])
        try:
            numeric = float(raw.replace(",", "."))
            status = "published"
        except ValueError:
            numeric = None
            status = "suppressed" if raw.strip() in {"..", "...", "-"} else "invalid"
        period = str(dimensions.get("Tid") or manifest["income"]["year"])
        contents = str(dimensions.get("ContentsCode") or manifest["income"]["contents_code"])
        raw_values.append(
            {
                "dataset_code": manifest["income"]["dataset_code"],
                "source_row": source_row,
                "region_code": region,
                "time_code": period,
                "contents_code": contents,
                "raw_value": raw,
                "original_values": item,
            }
        )
        observations.append(
            _observation(
                statistics_role="income_statistics",
                area_role="regso_boundaries",
                area_type="regso",
                area_code=region,
                indicator_id="scb:income:median_earned",
                reference_period=period,
                dimensions=dimensions,
                raw_value=raw,
                numeric_value=numeric,
                source_unit="tkr/year",
                normalized_value=None if numeric is None else numeric * 1000.0,
                normalized_unit="SEK/year",
                normalization_method="multiply_by_1000",
                value_status=status,
                provenance={"source_row": source_row},
            )
        )
    raw_responses = [
        {
            "dataset_code": manifest["income"]["dataset_code"],
            "request_sha256": manifest["income"]["request_sha256"],
            "request_json": income_request,
            "response_sha256": manifest["income"]["response_sha256"],
            "response_json": income_document,
            "retrieved_at": manifest["retrieved_at"],
        }
    ]
    return {
        "areas": areas,
        "raw_areas": raw_areas,
        "raw_population": raw_population,
        "raw_pxweb_responses": raw_responses,
        "raw_pxweb_values": raw_values,
        "indicators": sorted(indicators.values(), key=lambda row: row["indicator_id"]),
        "observations": observations,
    }


def _observation(**values: Any) -> dict[str, Any]:
    dimensions = values["dimensions"]
    identity = {
        "statistics_role": values["statistics_role"],
        "area_type": values["area_type"],
        "area_code": values["area_code"],
        "indicator_id": values["indicator_id"],
        "reference_period": values["reference_period"],
        "dimensions": dimensions,
    }
    return {
        **values,
        "observation_id": content_hash(identity),
        "dimension_hash": content_hash(dimensions),
    }


def derive_area_relations(
    buildings: Iterable[Mapping[str, Any]], areas: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Preserve all positive overlaps and make the primary decision explicit."""
    by_type: dict[str, list[Mapping[str, Any]]] = {}
    for area in areas:
        by_type.setdefault(str(area["area_type"]), []).append(area)
    trees = {
        kind: shapely.STRtree([area["geometry"] for area in records])
        for kind, records in by_type.items()
    }
    output: list[dict[str, Any]] = []
    for building in buildings:
        geometry = building["geometry"]
        if geometry is None or geometry.is_empty or not geometry.is_valid or geometry.area <= 0:
            continue
        point = shapely.point_on_surface(geometry)
        geometry_status = str(building.get("geometry_status") or "resolved")
        for area_type, candidates in by_type.items():
            matches = []
            for index in trees[area_type].query(geometry, predicate="intersects"):
                area = candidates[int(index)]
                area_geometry = area["geometry"]
                intersection = geometry.intersection(area_geometry)
                if intersection.is_empty or intersection.area <= 0:
                    continue
                matches.append(
                    {
                        "area": area,
                        "intersection_area_m2": float(intersection.area),
                        "footprint_fraction": float(intersection.area / geometry.area),
                        "contains_reference_point": bool(area_geometry.covers(point)),
                    }
                )
            matches.sort(
                key=lambda row: (-row["footprint_fraction"], str(row["area"]["area_code"]))
            )
            point_matches = [row for row in matches if row["contains_reference_point"]]
            primary = point_matches[0] if len(point_matches) == 1 else None
            ambiguous = len(point_matches) > 1
            if area_type == "population_grid_1km":
                primary = matches[0] if matches else None
                ambiguous = (
                    len(matches) > 1
                    and abs(matches[0]["footprint_fraction"] - matches[1]["footprint_fraction"])
                    < 1e-12
                )
                if ambiguous:
                    primary = None
            if area_type != "regso" and primary is None and not ambiguous and matches:
                primary = matches[0]
                if (
                    len(matches) > 1
                    and abs(matches[0]["footprint_fraction"] - matches[1]["footprint_fraction"])
                    < 1e-12
                ):
                    primary = None
                    ambiguous = True
            for rank, match in enumerate(matches, 1):
                status = "ambiguous" if ambiguous else "resolved"
                reasons: list[str] = []
                if geometry_status != "resolved":
                    status = "geometry_unresolved"
                    reasons.append("building_geometry_unresolved")
                is_primary = primary is match and status == "resolved"
                if area_type == "municipality" and is_primary:
                    lm_code = str(building.get("municipality_code") or "").zfill(4)
                    if lm_code and str(match["area"]["area_code"]).zfill(4) != lm_code:
                        status = "mismatch"
                        is_primary = False
                        reasons.append("lm_scb_municipality_mismatch")
                output.append(
                    {
                        "building_id": building["building_id"],
                        "area_role": match["area"]["area_role"],
                        "area_type": area_type,
                        "area_code": match["area"]["area_code"],
                        "assignment_method": "point_on_surface_then_maximum_overlap",
                        "intersection_area_m2": match["intersection_area_m2"],
                        "footprint_fraction": match["footprint_fraction"],
                        "contains_reference_point": match["contains_reference_point"],
                        "rank": rank,
                        "is_primary": is_primary,
                        "resolution_status": status,
                        "reason_codes": reasons,
                        "evidence": {"policy_version": CONTEXT_POLICY_VERSION},
                    }
                )
    return output
