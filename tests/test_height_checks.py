from shapely.geometry import box

from epsm_db.enums import BuildingType
from epsm_db.height_checks import (
    body_height,
    height_findings,
    height_plausibility_field,
    register_living_area,
)
from epsm_db.records import BuildingRecord


def _record(height, *, kind="single_family", year=2023, living="150", purposes=None, size=12.0):
    footprint = box(0, 0, size, size)
    purposes = purposes if purposes is not None else (
        {"detail": "Småhus friliggande", "category": "Bostad",
         "original_values": {"Bostad/totalBoarea": [living]} if living else {}},
    )
    return BuildingRecord(
        "dataset",
        "house",
        BuildingType(kind),
        footprint,
        {"construction_year": year, "purposes": list(purposes)},
        components=({"component_id": 1, "geometry": footprint},),
        heights=(dict({"component_id": 1, "height_status": "determined"}, **height),),
        purposes=tuple(purposes),
    )


def codes(record):
    return {f["code"] for f in height_findings(record)}


def test_ordinary_bungalow_is_plausible():
    record = _record({"representative_height_m": 3.4, "roof_form": "unclear"})
    assert codes(record) == set()
    assert height_plausibility_field(record, [])["source_state"] == "plausible"


def test_tree_canopy_height_on_new_bungalow_is_flagged():
    # SE.07 pattern: 144 m2 footprint, 150 m2 living area, 15 m "eave" from trees.
    record = _record({"eave_height_m": 14.9, "ridge_height_m": 16.6, "roof_form": "pitched_ridge",
                      "representative_height_m": 15.2})
    found = codes(record)
    assert found == {"small_house_height_implausible", "small_house_height_exceeds_living_area"}
    findings = height_findings(record)
    assert height_plausibility_field(record, findings) == {
        "source_state": "implausible",
        "value": ["small_house_height_exceeds_living_area", "small_house_height_implausible"],
        "origin": "small-house body height vs storey bounds, register living area and survey date",
    }


def test_true_two_storey_house_is_plausible():
    # 144 m2 footprint, 2 storeys at 5.8 m, 240 m2 living area.
    record = _record({"representative_height_m": 5.8, "roof_form": "unclear"}, living="240")
    assert codes(record) == set()


def test_two_storeys_on_single_storey_living_area_is_flagged():
    record = _record({"representative_height_m": 5.8, "roof_form": "unclear"}, living="120")
    assert codes(record) == {"small_house_height_exceeds_living_area"}


def test_living_area_check_needs_register_area():
    record = _record({"representative_height_m": 5.8, "roof_form": "unclear"}, living=None)
    assert codes(record) == set()


def test_survey_before_construction_is_a_time_mismatch():
    record = _record(
        {"representative_height_m": 3.2, "roof_form": "unclear", "measured_at": "2019-05-02"}, year=2022
    )
    assert codes(record) == {"height_survey_before_construction"}
    later = _record(
        {"representative_height_m": 3.2, "roof_form": "unclear", "measured_at": "2023-05-02"}, year=2022
    )
    assert codes(later) == set()


def test_other_building_types_are_not_assessed():
    record = _record({"representative_height_m": 20.0}, kind="multi_family", purposes=(
        {"detail": "Flerbostadshus", "category": "Bostad", "original_values": {}},
    ))
    assert height_findings(record) == []
    assert height_plausibility_field(record, [])["source_state"] == "not_assessed"


def test_smahus_purpose_qualifies_even_without_single_family_type():
    record = _record({"representative_height_m": 12.0}, kind="multi_family", living=None, purposes=(
        {"detail": "Småhus radhus", "category": "Bostad", "original_values": {}},
    ))
    assert codes(record) == {"small_house_height_implausible"}


def test_body_height_uses_eave_only_for_supported_pitched_roofs():
    assert body_height({"roof_form": "pitched_ridge", "eave_height_m": 3.0, "ridge_height_m": 6.0,
                        "representative_height_m": 4.5, "height_status": "determined"}) == 3.0
    assert body_height({"roof_form": "unclear", "eave_height_m": 3.0,
                        "representative_height_m": 4.5, "height_status": "determined"}) == 4.5
    assert body_height({"height_status": "not_determined", "representative_height_m": 4.5}) is None


def test_register_living_area_reads_building_attributes():
    record = _record({"representative_height_m": 3.0}, living=None)
    record.facts["attributes"] = {"original_values": {"Byggnad/Bostad/totalBoarea": ["181"]}}
    assert register_living_area(record) == 181.0
