from itertools import pairwise
from math import hypot

import pytest
from shapely.ops import unary_union

from examples.dwelling_zone_experiment import (
    build_spec,
    dwelling_chunks,
    require_physical_partition_walls,
    solid_dwelling_compiler,
)
from examples.service_core_experiment import choose_core, synthetic_cases


def prepared(name):
    footprint = synthetic_cases()[name]
    _, regions = choose_core(footprint)
    core = next(r.polygon for r in regions if r.kind == "core")
    return footprint, core


def test_remainders_are_distributed_without_losing_area():
    footprint, core = prepared("regular_400")
    report, units = dwelling_chunks(footprint, core)
    assert report["accepted"] and len(units) == 5
    assert all(p.area == pytest.approx(72) for p in units)
    assert unary_union([core, *units]).symmetric_difference(footprint).area < 1e-6


def test_rotation_search_avoids_narrow_units():
    footprint, core = prepared("rectangular_450")
    report, units = dwelling_chunks(footprint, core)
    assert report["accepted"] and len(units) == 6
    assert report["phase_degrees"] >= 20
    assert all(
        m["width_retention"] >= 0.95 and m["frontage_m"] >= 1.2 and m["access_m"] >= 0.9
        for m in report["units"]
    )


def test_large_floorplate_has_target_sized_connected_zones():
    footprint, core = prepared("large_1200")
    report, units = dwelling_chunks(footprint, core)
    assert report["accepted"] and len(units) == 16
    assert all(p.geom_type == "Polygon" and p.area == pytest.approx(67.5) for p in units)
    assert report["phase_degrees"] > 0
    assert all(
        hypot(a[0] - b[0], a[1] - b[1]) >= 0.010001
        for p in units
        for a, b in pairwise(p.exterior.coords)
    )


def test_strict_65_to_70_band_cannot_hide_the_integer_count_problem():
    footprint, core = prepared("regular_400")
    report, units = dwelling_chunks(footprint, core, min_area_m2=65, max_area_m2=70)
    assert not report["accepted"] and units is None
    assert report["reason"] == "unit_area_outside_sensitivity_band"


def test_virtual_dwelling_walls_are_rejected():
    with pytest.raises(ValueError, match="physical partition walls"):
        solid_dwelling_compiler(None, solid=False)
    with pytest.raises(ValueError, match="physical partition walls"):
        require_physical_partition_walls({"Construction:AirBoundary": {"partition": {}}})


def test_default_compiler_emits_real_paired_wall_constructions():
    import os

    epw = os.environ.get("UBEM_TEST_EPW")
    if not epw:
        pytest.skip("Set UBEM_TEST_EPW for native compiler verification")
    footprint, core = prepared("rectangular_450")
    _, units = dwelling_chunks(footprint, core)
    spec = build_spec("physical_wall_check", footprint, core, units, "chunks_solid", epw)
    model, report = solid_dwelling_compiler(os.environ.get("ENERGYPLUS_EXE")).compile(spec)
    assert not model.get("Construction:AirBoundary")
    assert require_physical_partition_walls(model) > 0
    assert report.data["dwelling_experiment"]["physical_interior_wall_pairs"] > 0
    assert report.data["dwelling_experiment"]["solid_unit_partitions"]
