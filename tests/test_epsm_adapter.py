from dataclasses import replace
from pathlib import Path

import pytest
from test_record_api import make_record

from sweden_ubem.enums import BuildingType
from sweden_ubem.epsm import EPSMBuilding, ModelAssemblyError, ModelPolicy
from sweden_ubem.weather import WeatherReference


@pytest.fixture(scope="module")
def weather():
    # Compilation checks do not simulate, but do require a complete annual EPW.
    import os

    path = os.environ.get("UBEM_TEST_EPW")
    if not path:
        pytest.skip("Set UBEM_TEST_EPW for optional EPSM compiler integration")
    return WeatherReference.from_epw(path, calendar_year=2023)


def test_suspect_storeys_are_inferred_without_source_mutation(weather):
    record = make_record(storeys=23)
    building, receipt = EPSMBuilding.from_record(record, weather=weather)
    assert record.storeys == 23
    assert building.spec.geometry.parts[0].floors == 1
    assert building.spec.geometry.footprint_area_m2 == 100
    assert building.spec.load_reference_area_m2 == 90
    assert receipt.fields["storeys"]["model_value"] == 1
    assert receipt.ready_for_simulation
    assert building.spec.use.dhw_kwh_m2_year == 20
    assert any("raw reported count retained" in a for a in receipt.assumptions)
    assert not record.receipt.ready_for_simulation


def test_full_basement_and_reference_area_remain_distinct(weather):
    record = make_record(basement=1)
    building, receipt = EPSMBuilding.from_record(record, weather=weather)
    assert building.spec.geometry.conditioned_floor_area_m2 == 200
    assert building.spec.load_reference_area_m2 == 90
    assert receipt.identities["load_to_physical_area_ratio"] == 0.45
    assert building.spec.zone_uses[0].zone_kind == "basement"


def test_multifamily_native_area_infiltration_and_dhw(weather):
    record = make_record(type=BuildingType.RESIDENTIAL_MULTIFAMILY)
    building, receipt = EPSMBuilding.from_record(record, weather=weather)
    assert building.spec.use.dhw_kwh_m2_year == 25
    assert building.spec.airflow.envelope_infiltration_l_s_m2 is not None
    assert building.spec.airflow.envelope_infiltration_ach == 0
    assert receipt.ready_for_simulation


def test_mixed_fractional_programmes_compile_on_common_building_controls(weather):
    record = make_record(programmes={"EgenAtempBostad": 80, "EgenAtempKontor": 20})
    building, receipt = EPSMBuilding.from_record(record, weather=weather)
    parts = receipt.identities["programme_blend"]["components"]
    assert [p["area_fraction"] for p in parts] == [0.8, 0.2]
    assert receipt.ready_for_simulation
    assert building.spec.load_reference_area_m2 == 90


@pytest.mark.parametrize(
    "kind,share",
    [(BuildingType.OFFICE, "EgenAtempKontor"), (BuildingType.SCHOOL, "EgenAtempSkolor")],
)
def test_nonresidential_profiles_have_explicit_calendars(weather, kind, share):
    model, receipt = EPSMBuilding.from_record(
        make_record(type=kind, programmes={share: 100}), weather=weather
    )
    assert receipt.ready_for_simulation
    assert model.spec.airflow.topology == "FTX"
    assert model.spec.use.occupancy_category == "non_residential"


def test_missing_area_uses_geometry_without_creating_reported_atemp(weather):
    record = make_record()
    record = replace(record, certificates=(dict(record.certificates[0], loaded_building_count=2),))
    model, receipt = EPSMBuilding.from_record(record, weather=weather)
    assert record.atemp_m2 is None
    assert model.spec.load_reference_area_m2 == 100
    assert receipt.fields["atemp_m2"]["model_state"] == "assumed_geometric_load_area"


def test_batch_failures_are_retained_and_weather_checksum_is_checked(weather, tmp_path):
    invalid = make_record(basement=3)
    good = make_record()
    buildings, receipts = EPSMBuilding.from_records([invalid, good], weather=weather)
    assert len(buildings) == 1 and len(receipts) == 2
    assert receipts[0].errors and receipts[1].ready_for_simulation
    changed = replace(weather, sha256="0" * 64)
    with pytest.raises(ModelAssemblyError, match="checksum"):
        EPSMBuilding.from_record(good, weather=changed)
    paths = buildings[0].write(tmp_path / "building.json")
    assert all(Path(p).exists() for p in paths)
    with pytest.raises(FileExistsError):
        buildings[0].write(tmp_path / "building.json")


def test_preflight_false_does_not_claim_simulation_readiness(weather):
    _, receipt = EPSMBuilding.from_record(make_record(), weather=weather, preflight=False)
    assert not receipt.ready_for_simulation


def test_invalid_policy_rejected():
    with pytest.raises(ValueError):
        ModelPolicy(ftx_recovery=1.1)


def test_roof_geometry_is_invariant_to_absolute_map_coordinates():
    import math

    from shapely.affinity import translate

    from sweden_ubem.epsm import _geometry
    from sweden_ubem.records import Receipt

    record = make_record()
    heights = dict(record.heights[0], roof_form="pitched_ridge", eave_height_m=3, ridge_height_m=5)
    local = replace(record, heights=(heights,))
    moved_geometry = translate(record.geometry, xoff=321250, yoff=6474490)
    moved = replace(
        local,
        geometry=moved_geometry,
        components=(dict(record.components[0], geometry=moved_geometry),),
    )
    local_receipt, moved_receipt = Receipt("test"), Receipt("test")
    a = _geometry(local, ModelPolicy(), local_receipt, {})
    b = _geometry(moved, ModelPolicy(), moved_receipt, {})
    assert a.parts[0].roof.pitch_deg == pytest.approx(math.degrees(math.atan(2 / 5)))
    assert b.parts[0].roof.pitch_deg == pytest.approx(a.parts[0].roof.pitch_deg)
    assert b.parts[0].footprint.exterior == a.parts[0].footprint.exterior
    assert b.conditioned_floor_area_m2 == a.conditioned_floor_area_m2
    origin = moved_receipt.identities["geometry_coordinate_transform"]["translation_origin_xy"]
    assert origin == [321255, 6474495]
    assert moved.geometry.equals(moved_geometry)


def test_components_keep_one_common_local_frame():
    from shapely.affinity import translate
    from shapely.ops import unary_union

    from sweden_ubem.epsm import _geometry
    from sweden_ubem.records import Receipt

    record = make_record()
    other = translate(record.geometry, xoff=12)
    record = replace(
        record,
        geometry=unary_union([record.geometry, other]),
        components=record.components
        + (dict(record.components[0], component_id="c2", geometry=other),),
        heights=record.heights + (dict(record.heights[0], component_id="c2"),),
    )
    geometry = _geometry(record, ModelPolicy(), Receipt("test"), {})
    bounds = [part.footprint.polygon.bounds for part in geometry.parts]
    assert bounds[1][0] - bounds[0][0] == 12
    assert geometry.footprint_area_m2 == 200
