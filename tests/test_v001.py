import json
from pathlib import Path
import pytest
import shapely
from fixture_factory import synthetic_sources, BUILDINGS
from sweden_ubem.epc import strict_value, selected_rows, observations
from sweden_ubem.dataset import primary_selection, selection_expansion, plan_dataset
from sweden_ubem.linkage import building_property_candidates
from sweden_ubem.cli import build_parser


def test_cli_has_dataset_not_toy():
    parser = build_parser()
    assert parser.parse_args(["dataset", "plan"]).municipality == "1485"
    with pytest.raises(SystemExit):
        parser.parse_args(["toy", "build"])


@pytest.mark.parametrize(
    "value,kind", [("1.2", "integer"), ("unknown", "boolean"), ("nan", "number"), ("inf", "number")]
)
def test_conversion_does_not_guess(value, kind):
    with pytest.raises(ValueError):
        strict_value(value, kind)


def test_property_union_does_not_double_count():
    geom = shapely.box(0, 0, 10, 10)
    result = building_property_candidates(
        [dict(building_id="b", linkage_geometry=geom, linkage_eligible=True)],
        [dict(property_unit_id="p", geometry=geom), dict(property_unit_id="p", geometry=geom)],
    )
    assert result[0]["overlap_fraction"] == 1
    assert result[0]["intersection_area_m2"] == 100


def test_selection_completes_original_certificates_not_recursive(tmp_path):
    s = synthetic_sources(tmp_path)
    rows, ids = primary_selection(s, "1485")
    assert len(rows) == 3 and len(ids) == 2
    complete, primary, raw, expansion = selection_expansion(s, rows, ids, "1485")
    assert {r["building_id"] for r in complete} == set(BUILDINGS)
    assert expansion["supporting_buildings"] == 1
    assert expansion["original_certificates"] == ["fore_v260:10"]
    assert {r["original_values"]["FormularId"] for r in raw} == {"10"}
    assert plan_dataset(s, "1485")["primary_buildings"] == 2


def test_epc_repetition_preserves_raw_locators_not_quantity_multiplication(tmp_path):
    s = synthetic_sources(tmp_path)
    rows = list(selected_rows(s.path("epc_raw_root"), "1485"))
    mapping = dict(
        regime="fore_v260",
        source_field="EgenAtemp",
        owner="certificate",
        value_type="number",
        review_status="verified",
    )
    normalized = observations(rows, [mapping])
    atemp = next(x for x in normalized if x["source_field"] == "EgenAtemp")
    assert atemp["normalized_value"] == {"value": 200}
    assert len(atemp["source_locators"]) == 3
    energy = next(x for x in normalized if x["source_field"] == "EgiEnergiPrestanda")
    assert energy["normalized_value"] is None


def test_schema_does_not_include_simulation_or_graph_tables():
    sql = "\n".join(p.read_text() for p in Path("sql/migrations").glob("*.sql"))
    assert "raw_epsm" not in sql and "epsm_readiness" not in sql
    assert "SELECT *" not in sql.replace("never SELECT *", "")
    assert "construction_year integer" in sql
    assert "input_geometry_hash" in sql


def test_scope_rules_cannot_store_sql():
    from sweden_ubem.scopes import validate_scope

    with pytest.raises(ValueError):
        validate_scope({"sql": "SELECT * FROM stock.building"})
    with pytest.raises(ValueError):
        validate_scope({"include_support": "false"})


def test_unassessed_label_does_not_withhold_valid_geometry():
    from sweden_ubem.components import assemble_building_snapshot

    result = assemble_building_snapshot(
        "b",
        [
            dict(
                building_id="b",
                geometry=shapely.box(0, 0, 1, 1),
                representation_status="not_assessed",
            )
        ],
    )
    assert result["linkage_eligible"]


def test_measurement_row_requires_matching_geometry(tmp_path):
    from sweden_ubem.dataset import measurement_record
    from sweden_ubem.components import normalized_geometry_hash

    synthetic_sources(tmp_path)
    geom = shapely.box(0, 0, 10, 10)
    locator = dict(
        building_id=BUILDINGS[0],
        input_geometry_hash=normalized_geometry_hash(geom),
        baseline_row_offset=0,
    )
    payload, geometry, ordinal = measurement_record(tmp_path / "measurement.parquet", locator, {})
    assert ordinal == "0" and payload["ground_elevation_m"] == "10.0"
    with pytest.raises(ValueError, match="geometry locator"):
        measurement_record(
            tmp_path / "measurement.parquet", {**locator, "input_geometry_hash": "0" * 64}, {}
        )
    with pytest.raises(ValueError, match="out of range"):
        measurement_record(
            tmp_path / "measurement.parquet", {**locator, "baseline_row_offset": 1}, {}
        )


def test_selection_rejects_incomplete_register_manifest(tmp_path):
    s = synthetic_sources(tmp_path)
    path = s.components() / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["partition_sha256"]["missing.parquet"] = "0" * 64
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="partitions"):
        primary_selection(s, "1485")


def test_boundary_is_pinned_regso_union_without_simplified_map(tmp_path):
    import json
    import shutil
    from sweden_ubem.dataset import municipality_rows
    from sweden_ubem.hashing import sha256_file

    s = synthetic_sources(tmp_path)
    s.path("scb_municipality_boundaries_primary").unlink()
    snapshot = tmp_path / "boundary-snapshot"
    shutil.copytree(s.path("scb_snapshot_selected"), snapshot)
    manifest = json.loads((snapshot / "manifest.json").read_text())
    response = snapshot / manifest["regso"]["response_file"]
    geoms = [shapely.box(0, 0, 10, 10), shapely.box(10, 0, 20, 10), shapely.box(30, 0, 40, 10)]
    features = [
        dict(
            type="Feature",
            properties=dict(regsokod=f"R{i}", kommunkod="1485"),
            geometry=shapely.geometry.mapping(g),
        )
        for i, g in enumerate(geoms)
    ]
    response.write_text(
        json.dumps(dict(type="FeatureCollection", features=features, numberMatched=3))
    )
    manifest["regso"].update(
        feature_count=3, area_codes=["R0", "R1", "R2"], response_sha256=sha256_file(response)
    )
    (snapshot / "manifest.json").write_text(json.dumps(manifest))
    result = municipality_rows(snapshot, "1485")
    assert result.crs.to_epsg() == 3006
    assert result.geometry.iloc[0].equals(shapely.union_all(geoms))
    assert result.attrs["boundary_kind"] == "union_of_regso"
    with pytest.raises(ValueError, match="municipality"):
        municipality_rows(snapshot, "1484")
    document = json.loads(response.read_text())
    document["numberMatched"] = 4
    response.write_text(json.dumps(document))
    manifest["regso"]["response_sha256"] = sha256_file(response)
    (snapshot / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Incomplete"):
        municipality_rows(snapshot, "1485")


def test_fast_xml_selection_retains_namespace_and_record_locator(tmp_path):
    from sweden_ubem.lm_xml import load_selected_buildings

    path = tmp_path / "records.xml"
    path.write_text(
        '<ByggnadResponse xmlns="urn:lm" xmlns:gml="http://www.opengis.net/gml/3.2"><ByggnadMember><Byggnad gml:id="x"><objektidentitet>other</objektidentitet></Byggnad></ByggnadMember><ByggnadMember><Byggnad><objektidentitet>b</objektidentitet><Bostad><andamal>Flerfamiljshus</andamal></Bostad></Byggnad></ByggnadMember></ByggnadResponse>'
    )
    result = load_selected_buildings(
        [dict(building_id="b", source_file="records.xml", source_record=2)], tmp_path
    )
    assert result[0]["purposes"][0]["category"] == "Bostad"
    with pytest.raises(ValueError, match="locator mismatch"):
        load_selected_buildings(
            [dict(building_id="b", source_file="records.xml", source_record=1)], tmp_path
        )
