import shapely

from sweden_ubem.linkage import building_property_candidates, derive_epc_candidates


def test_property_parts_aggregate_by_identity_and_cardinality_is_global():
    buildings = [
        {
            "building_id": "b1",
            "house_number": 1,
            "linkage_eligible": True,
            "linkage_geometry": shapely.box(0, 0, 2, 1),
        },
        {
            "building_id": "b2",
            "house_number": 1,
            "linkage_eligible": True,
            "linkage_geometry": shapely.box(0, 2, 2, 3),
        },
    ]
    parts = [
        {"property_unit_id": "p1", "geometry": shapely.box(0, 0, 1, 4)},
        {"property_unit_id": "p1", "geometry": shapely.box(1, 0, 2, 4)},
    ]
    overlaps = building_property_candidates(buildings, parts)
    assert len(overlaps) == 2
    assert all(row["overlap_fraction"] == 1.0 for row in overlaps)
    relations = derive_epc_candidates(
        buildings,
        overlaps,
        [
            {
                "property_unit_id": "p1",
                "municipality_code": "1480",
                "designation_normalized": "TEST 1:1",
                "match_key": "1480|TEST 1:1",
            }
        ],
        [
            {
                "epc_subject_id": "s1",
                "formular_id": 10,
                "property_match_key": "1480|TEST 1:1",
                "house_number": 1,
            }
        ],
    )
    assert {row["decision"] for row in relations} == {"accepted"}
    assert {row["certificate_to_footprint_count"] for row in relations} == {2}


def test_linkage_rejects_house_number_mismatch():
    building = {
        "building_id": "b",
        "house_number": 2,
        "linkage_eligible": True,
        "linkage_geometry": shapely.box(0, 0, 1, 1),
    }
    overlaps = building_property_candidates(
        [building], [{"property_unit_id": "p", "geometry": shapely.box(-1, -1, 2, 2)}]
    )
    result = derive_epc_candidates(
        [building],
        overlaps,
        [{"property_unit_id": "p", "match_key": "1480|P"}],
        [
            {
                "epc_subject_id": "s",
                "formular_id": 1,
                "property_match_key": "1480|P",
                "house_number": 1,
            }
        ],
    )
    assert result[0]["decision"] == "rejected"
    assert result[0]["rejection_reason"] == "house_number_disagreement"
