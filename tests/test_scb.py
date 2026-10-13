import json
from pathlib import Path

import geopandas as gpd
import shapely
import pytest

from sweden_ubem.context import derive_area_relations, project_scb_snapshot
from sweden_ubem.scb import POPULATION_FIELDS, acquire_scb_snapshot, validate_scb_snapshot


def _population_fixture(path: Path) -> None:
    values = {
        "objectid": [1, 2],
        "rutid_inspire": ["i1", "i2"],
        "rutid_scb": ["g1", "g2"],
        "rutstorl": [1000, 1000],
        "referenstid": ["20251231", "20251231"],
    }
    for field in POPULATION_FIELDS:
        values[field] = [10, None] if field == "beftotalt" else [1, None]
    frame = gpd.GeoDataFrame(
        values, geometry=[shapely.box(0, 0, 10, 10), shapely.box(10, 0, 20, 10)], crs=3006
    )
    frame.to_file(path, layer="befolkning_1km_2025", driver="GPKG")


def test_acquisition_is_content_addressed_and_projection_is_offline(tmp_path):
    population = tmp_path / "population.gpkg"
    _population_fixture(population)
    calls = []

    def transport(method, url, body, headers):
        calls.append((method, url, body))
        if method == "GET":
            document = {
                "type": "FeatureCollection",
                "timeStamp": f"attempt-{len(calls)}",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "regsokod": "R1",
                            "regsonamn": "Fixture",
                            "kommunkod": "1480",
                        },
                        "geometry": shapely.geometry.mapping(shapely.box(0, 0, 20, 10)),
                    }
                ],
            }
        else:
            request = json.loads(body)
            assert request["query"][0]["selection"]["values"] == ["R1_RegSO2025"]
            document = {
                "columns": [
                    {"code": "Region"},
                    {"code": "InkomstTyp"},
                    {"code": "Kon"},
                    {"code": "ContentsCode"},
                    {"code": "Tid"},
                ],
                "data": [
                    {
                        "key": ["R1_RegSO2025", "SaFörInk", "1+2", "0000089U", "2024"],
                        "values": ["321"],
                    }
                ],
            }
        return json.dumps(document).encode(), {"content-type": "application/json"}

    first = acquire_scb_snapshot(
        population_grid=population, output_root=tmp_path / "snapshots", transport=transport
    )
    second = acquire_scb_snapshot(
        population_grid=population, output_root=tmp_path / "snapshots", transport=transport
    )
    assert first["snapshot_id"] == second["snapshot_id"]
    assert validate_scb_snapshot(Path(first["path"]))["ready"] is True
    assert len(calls) == 4

    projected = project_scb_snapshot(Path(first["path"]), shapely.box(0, 0, 20, 10))
    assert {row["area_type"] for row in projected["areas"]} == {"regso", "population_grid_1km"}
    income = next(
        row
        for row in projected["observations"]
        if row["indicator_id"] == "scb:income:median_earned"
    )
    assert income["numeric_value"] == 321
    assert income["normalized_value"] == 321000
    protected = [
        row for row in projected["observations"] if row["value_status"] == "published_protected"
    ]
    assert protected

    # Immutable snapshots retain their original absolute population-grid path.
    # The bundle inventory resolves it after the original root disappears.
    from sweden_ubem.hashing import canonical_json, sha256_file

    manifest_path = Path(first["path"]) / "manifest.json"
    original_manifest = manifest_path.read_bytes()
    inventory = {"files": [
        {"original": str(p), "path": str(p.relative_to(tmp_path)),
         "sha256": sha256_file(p), "bytes": p.stat().st_size}
        for p in (population, manifest_path)
    ]}
    (tmp_path / "manifests").mkdir()
    (tmp_path / "manifests/file-inventory.json").write_text(json.dumps(inventory))
    old_root = tmp_path
    moved = tmp_path.with_name(tmp_path.name + "-relocated")
    tmp_path.rename(moved)
    new_snapshot = moved / manifest_path.parent.relative_to(old_root)
    assert not old_root.exists()
    assert (new_snapshot / "manifest.json").read_bytes() == original_manifest
    assert validate_scb_snapshot(new_snapshot)["ready"] is True
    relocated = project_scb_snapshot(new_snapshot, shapely.box(0, 0, 20, 10))
    assert canonical_json(relocated["areas"]) == canonical_json(projected["areas"])
    assert [(r["indicator_id"], r["normalized_value"], r["value_status"])
            for r in relocated["observations"]] == [
        (r["indicator_id"], r["normalized_value"], r["value_status"])
        for r in projected["observations"]
    ]
    with (moved / "population.gpkg").open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError, match="checksum differs"):
        validate_scb_snapshot(new_snapshot)


def test_assignment_preserves_boundary_tie_and_missing_area():
    buildings = [
        {
            "building_id": "tie",
            "geometry": shapely.box(0, 0, 20, 10),
            "geometry_status": "resolved",
            "municipality_code": "1480",
        },
        {
            "building_id": "outside",
            "geometry": shapely.box(30, 0, 40, 10),
            "geometry_status": "resolved",
            "municipality_code": "1480",
        },
    ]
    areas = [
        {
            "area_role": "population_grid",
            "area_type": "population_grid_1km",
            "area_code": "left",
            "geometry": shapely.box(0, 0, 10, 10),
        },
        {
            "area_role": "population_grid",
            "area_type": "population_grid_1km",
            "area_code": "right",
            "geometry": shapely.box(10, 0, 20, 10),
        },
    ]
    relations = derive_area_relations(buildings, areas)
    tie = [row for row in relations if row["building_id"] == "tie"]
    assert len(tie) == 2
    assert all(row["resolution_status"] == "ambiguous" and not row["is_primary"] for row in tie)
    assert not [row for row in relations if row["building_id"] == "outside"]
