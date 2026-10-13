"""Usability guard checks for the optional service-core experiment."""

from dataclasses import replace

import pytest
from shapely.geometry import box

from examples.service_core_experiment import CorePolicy, choose_core, opened_area_fraction


def test_small_footprints_are_not_given_a_service_core():
    report, plan = choose_core(box(0, 0, 10, 12))
    assert report["reason"] == "small_footprint" and plan is None


def test_small_service_area_and_shallow_habitable_band_are_rejected():
    report, _ = choose_core(box(0, 0, 20, 20), replace(CorePolicy(), service_fraction=0.02))
    assert report["reason"] == "small_service_area"
    report, _ = choose_core(box(0, 0, 5, 45))
    assert report["reason"] == "shallow_habitable_band"


def test_narrow_service_core_cannot_pass_on_area_alone():
    report, plan = choose_core(box(0, 0, 10, 25))
    assert report["actual_core_m2"] == pytest.approx(25)
    assert report["perimeter_depth_m"] > 2.5
    assert report["reason"] == "narrow_service_core" and plan is None


def test_regular_layout_preserves_footprint_and_target_area():
    report, plan = choose_core(box(0, 0, 20, 20))
    assert report["accepted"] and plan
    assert report["actual_core_m2"] == pytest.approx(40)
    assert sum(p.polygon.area for p in plan) == pytest.approx(400)
    assert report["core_width_area_retention"] == pytest.approx(1)
    assert report["habitable_width_area_retention"] == pytest.approx(1)


def test_width_screen_detects_a_thin_appendage_not_just_a_large_bounding_box():
    shape = box(0, 0, 5, 5).union(box(5, 2, 20, 3))
    assert opened_area_fraction(shape, 2) < 0.95
