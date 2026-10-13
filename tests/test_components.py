import shapely
import pytest

from sweden_ubem.components import (
    assemble_building_snapshot,
    component_identifier,
    normalized_geometry_hash,
    relation_candidates,
)


def test_component_identity_is_normalized_2d_geometry_hash():
    polygon = shapely.Polygon([(0, 0, 4), (1, 0, 5), (1, 1, 6), (0, 0, 4)])
    reversed_polygon = shapely.Polygon(list(reversed([(0, 0), (1, 0), (1, 1), (0, 0)])))
    assert normalized_geometry_hash(polygon) == normalized_geometry_hash(reversed_polygon)
    assert component_identifier("lm-uuid", polygon) == "lm-uuid:" + normalized_geometry_hash(
        polygon
    )


def test_snapshot_retains_components_and_accepts_source_representation_labels():
    rows = [
        {
            "building_id": "b",
            "geometry": shapely.box(0, 0, 1, 1),
            "representation_status": "resolved_component",
        },
        {
            "building_id": "b",
            "geometry": shapely.box(1, 0, 2, 1),
            "representation_status": "unresolved_overlap",
        },
    ]
    snapshot = assemble_building_snapshot("b", rows)
    assert snapshot["component_count"] == 2
    assert snapshot["resolution_status"] == "resolved"
    assert snapshot["linkage_geometry"].area == 2


@pytest.mark.parametrize("second", [shapely.box(.95, 0, 2, 1), shapely.box(3, 0, 4, 1)])
def test_overlapping_and_disconnected_parts_support_property_and_epc_links(second):
    from sweden_ubem.linkage import building_property_candidates, derive_epc_candidates
    rows = [dict(building_id="b", component_id=str(i), geometry=g,
                 representation_status="not_assessed")
            for i,g in enumerate([shapely.box(0,0,1,1), second])]
    before = [normalized_geometry_hash(r["geometry"]) for r in rows]
    snapshot = assemble_building_snapshot("b", rows)
    snapshot["house_number"] = 7
    assert snapshot["linkage_eligible"]
    assert snapshot["linkage_geometry"].equals(shapely.union_all([r["geometry"] for r in rows]))
    assert [normalized_geometry_hash(r["geometry"]) for r in rows] == before
    candidates = building_property_candidates([snapshot], [dict(property_unit_id="p",geometry=shapely.box(-1,-1,5,2))])
    result = derive_epc_candidates([snapshot], candidates,
        [dict(property_unit_id="p", municipality_code="1485", designation_raw="TOWN 1:1")],
        [dict(epc_subject_id="s", formular_id=1, property_match_key="1485|TOWN 1:1", house_number=7)])
    assert result[0]["decision"] == "accepted"
    assert all(r["decision_status"] == "accepted_union" for r in relation_candidates(rows))


@pytest.mark.parametrize("geometry", [shapely.Point(0,0), shapely.Polygon(),
    shapely.Polygon([(0,0),(1,1),(0,1),(1,0),(0,0)]), None])
def test_invalid_nonpolygon_and_empty_source_geometries_remain_ineligible(geometry):
    assert not assemble_building_snapshot("b", [dict(building_id="b", geometry=geometry)])["linkage_eligible"]
