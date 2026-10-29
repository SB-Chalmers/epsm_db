"""Offline geometry fixtures from the Uddevalla EPC baseline failures."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from shapely.geometry import shape
from shapely.ops import unary_union
from test_record_api import make_record

from sweden_ubem.enums import BuildingType
from sweden_ubem.epsm import ModelPolicy, _geometry
from sweden_ubem.records import Receipt

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures/uddevalla_failure_geometry.json").read_text()
)


def fixture_record(name):
    source = FIXTURES[name]
    components = []
    heights = []
    for item in source["components"]:
        components.append(
            {
                "component_id": item["component_id"],
                "geometry_hash": item["geometry_hash"],
                "geometry": shape(item["geometry"]),
                "outline_labels": ["Fasad"],
            }
        )
        heights.append(
            {
                "component_id": item["component_id"],
                "input_geometry_hash": item["geometry_hash"],
                "height_status": "determined",
                "roof_form": item["roof_form"],
                "eave_height_m": item["eave_height_m"],
                "representative_height_m": item["representative_height_m"],
                "ridge_height_m": item["ridge_height_m"],
                "details": {"evidence_resolution": "resolved"},
            }
        )
    record = make_record(
        storeys=source["storeys"],
        basement=source["basement_storeys"],
        type=BuildingType(source["type"]),
    )
    observations = tuple(
        dict(row, raw_values=[str(source["atemp_m2"])])
        if row["source_field"] == "EgenAtemp"
        else row
        for row in record.observations
    )
    return replace(
        record,
        geometry=unary_union([component["geometry"] for component in components]),
        components=tuple(components),
        heights=tuple(heights),
        observations=observations,
    )


@pytest.mark.parametrize(
    "name",
    [
        "overlap_school_1976",
        "overlap_school_1961",
        "overlap_office_1986",
        "overlap_office_1996",
        "overlap_multifamily_1976",
    ],
)
def test_real_components_have_bounded_resolved_ownership(name):
    record = fixture_record(name)
    receipt = Receipt("fixture")
    geometry = _geometry(record, ModelPolicy(), receipt, {})
    parts = geometry.parts
    assert receipt.identities["component_overlap_resolution"]
    assert all(
        a.footprint.polygon.intersection(b.footprint.polygon).area < 1e-8
        for i, a in enumerate(parts)
        for b in parts[i + 1 :]
    )
    assert geometry.conditioned_floor_area_m2 > 0
    assert all(component["geometry"].is_valid for component in record.components)


def test_minor_fragment_is_omitted_without_dropping_meaningful_components():
    record = fixture_record("overlap_office_1986")
    receipt = Receipt("fixture")
    geometry = _geometry(record, ModelPolicy(), receipt, {})
    omitted = receipt.identities["omitted_minor_components"]
    assert len(omitted) == 1
    assert omitted[0]["source_area_m2"] < 5
    assert omitted[0]["source_area_m2"] / record.geometry.area < 0.01
    assert len(geometry.parts) == 4
    assert min(part.footprint.area_m2 for part in geometry.parts) > 5


def test_near_flat_ridge_uses_compact_roof_without_changing_source():
    record = fixture_record("near_flat_school")
    receipt = Receipt("fixture")
    geometry = _geometry(record, ModelPolicy(), receipt, {})
    assert geometry.parts[0].roof.shape == "flat"
    assert record.heights[0]["roof_form"] == "pitched_ridge"
    assert any("reported ridge/eave rise" in note for note in receipt.assumptions)


def test_near_flat_multifamily_does_not_create_skinny_attic():
    record = fixture_record("near_flat_multifamily_attic_warning")
    receipt = Receipt("fixture")
    geometry = _geometry(record, ModelPolicy(), receipt, {})
    assert 0 < record.heights[0]["ridge_height_m"] - record.heights[0]["eave_height_m"] < 0.1
    assert geometry.parts[0].roof.shape == "flat"
    assert geometry.parts[0].roof.topology == "compact"
    assert any("compact roof" in note for note in receipt.assumptions)


def test_low_eave_uses_plausible_representative_height():
    record = fixture_record("low_eave_multifamily")
    receipt = Receipt("fixture")
    geometry = _geometry(record, ModelPolicy(), receipt, {})
    assert geometry.parts[0].floors == 2
    assert geometry.parts[0].roof.shape == "flat"
    assert geometry.parts[0].storey_elevations_m[-1] == pytest.approx(4.55)
    assert record.heights[0]["eave_height_m"] < 2


def test_two_unheated_basements_are_aggregated_when_atemp_fits_above_ground():
    record = fixture_record("two_basements_multifamily")
    receipt = Receipt("fixture")
    geometry = _geometry(record, ModelPolicy(), receipt, {})
    assert geometry.parts[0].floors == 3
    assert geometry.basement_depth_m == 5
    assert not geometry.basement_conditioned
    assert geometry.conditioned_floor_area_m2 > record.atemp_m2
    assert any("aggregated unconditioned" in note for note in receipt.assumptions)


def test_material_component_overlap_still_requires_review():
    from epsm_core import BuildingPart, Footprint

    from sweden_ubem.epsm import _resolve_component_overlaps

    parts = (
        BuildingPart(identifier="a", footprint=Footprint.rectangle(10, 10), storey_elevations_m=(0, 3)),
        BuildingPart(identifier="b", footprint=Footprint.rectangle(10, 10), storey_elevations_m=(0, 3)),
    )
    with pytest.raises(ValueError, match="exceeds bounded ownership repair"):
        _resolve_component_overlaps(parts, ModelPolicy(), Receipt("fixture"))
