"""Geometry invariants for the bounded fixture-layout experiment."""

import pytest
from shapely.affinity import rotate, translate
from shapely.geometry import GeometryCollection, LineString, box
from shapely.ops import unary_union

from examples.rectilinear_fixture_check import _pieces, rectilinear_units
from examples.service_core_experiment import choose_core


@pytest.mark.parametrize("width,length,expected_count", [(20, 20, 5), (15, 30, 6)])
def test_regular_layout_preserves_area_and_usable_access(width, length, expected_count):
    footprint = box(0, 0, width, length)
    _, regions = choose_core(footprint)
    core = next(r.polygon for r in regions if r.kind == "core")
    report, layout = rectilinear_units(footprint, core)
    assert report["accepted"] and report["unit_count"] == expected_count
    outline, service, units = layout
    assert unary_union([service, *units]).symmetric_difference(outline).area < 1e-6
    assert sum(u.area for u in units) + service.area == pytest.approx(footprint.area)
    for item in report["units"]:
        assert 55 <= item["area_m2"] <= 85
        assert item["frontage_m"] >= 1.2 and item["core_access_m"] >= 0.9
        assert item["width_retention"] >= 0.95


def test_large_end_caps_fail_without_discarding_area():
    footprint = box(0, 0, 30, 40)
    _, regions = choose_core(footprint)
    core = next(r.polygon for r in regions if r.kind == "core")
    report, layout = rectilinear_units(footprint, core)
    assert not report["accepted"] and layout is None
    assert report["attempts"] <= 40
    assert footprint.area == 1200


@pytest.mark.parametrize("east,north", [(0, 0), (500000, 6400000)])
def test_rotated_rectangle_safely_rejects_tiny_clip_edges(east, north):
    # Known conservative false negative: rotation creates very short clip edges.
    # Preserve its failure receipt until shared-boundary normalization is improved.
    footprint = translate(rotate(box(0, 0, 15, 30), 23, origin=(0, 0)), east, north)
    source = footprint.wkb
    _, regions = choose_core(footprint)
    core = next(r.polygon for r in regions if r.kind == "core")
    report, layout = rectilinear_units(footprint, core)
    assert not report["accepted"] and layout is None
    assert report["reason"] == "subcentimetre_edge"
    assert footprint.wkb == source


def test_zero_area_clip_remnants_are_not_thermal_spaces():
    polygon = box(0, 0, 3, 3)
    clipped = GeometryCollection([polygon, LineString([(3, 3), (4, 4)])])
    pieces = _pieces(clipped)
    assert len(pieces) == 1 and pieces[0].equals(polygon)
