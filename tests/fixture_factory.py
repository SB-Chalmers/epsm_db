"""Small synthetic sources only; no licensed national rows are checked in."""

import json
from pathlib import Path
from types import SimpleNamespace
import geopandas as gpd
import pyarrow as pa
import pyarrow.parquet as pq
import shapely
from sweden_ubem.components import normalized_geometry_hash
from sweden_ubem.hashing import sha256_file

BUILDINGS = [
    "00000000-0000-0000-0000-000000000001",
    "00000000-0000-0000-0000-000000000002",
    "00000000-0000-0000-0000-000000000003",
]


class FixtureSources:
    profile = "synthetic"
    root = None

    def __init__(self, paths):
        self.paths = paths
        self.inventory = SimpleNamespace(
            _by_key={"height_staging_catalogue": SimpleNamespace(expected_sha256=None)}
        )

    def path(self, key):
        return self.paths.get(key, Path("/does-not-exist") / key)

    def components(self):
        return self.path("tower_component_register_root")


def synthetic_sources(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    footprints = root / "buildings.gpkg"
    properties = root / "properties.gpkg"
    geometry = [
        shapely.box(0, 0, 10, 10),
        shapely.box(10, 0, 20, 10),
        shapely.box(40, 0, 50, 10),
        shapely.box(100, 0, 110, 10),
    ]
    owner = [BUILDINGS[0], BUILDINGS[0], BUILDINGS[1], BUILDINGS[2]]
    gpd.GeoDataFrame(
        {
            "objektidentitet": owner,
            "kommunkod": ["1485", "1485", "1485", "1484"],
            "objektversion": [1] * 4,
        },
        geometry=geometry,
        crs=3006,
    ).to_file(footprints, layer="byggnad")
    gpd.GeoDataFrame(
        {
            "objektidentitet": ["part1", "part2", "part3"],
            "registerenhetsreferens": ["p1", "p1", "p2"],
            "kommunkod": ["1485", "1485", "1484"],
            "trakt": ["TOWN", "TOWN", "ROAD"],
            "etikett": ["1:1"] * 3,
        },
        geometry=[
            shapely.box(-1, -1, 51, 11),
            shapely.box(-1, -1, 21, 11),
            shapely.box(99, -1, 111, 11),
        ],
        crs=3006,
    ).to_file(properties, layer="registerenhetsomradesyta")
    register = root / "register"
    (register / "components").mkdir(parents=True)
    rows = []
    lineage = []
    heights = []
    for fid, (bid, geom) in enumerate(zip(owner, geometry), 1):
        digest = normalized_geometry_hash(geom)
        cid = bid + ":" + digest
        state = "unresolved_overlap" if bid == BUILDINGS[1] else "resolved_component"
        rows.append(
            dict(
                component_id=cid,
                building_id=bid,
                geometry_hash=digest,
                geometry=geom.wkb,
                municipality_values=["1484" if bid == BUILDINGS[2] else "1485"],
                source_fids=[fid],
                outline_labels=["Fasad"],
                representation_status=state,
                selected_object_version=1,
                selected_revision_timestamp=None,
                owner_tile_id="t",
            )
        )
        lineage.append(
            dict(component_id=cid, building_id=bid, source_fid=fid, disposition="retained")
        )
        heights.append(
            dict(
                component_id=cid,
                building_id=bid,
                input_geometry_hash=digest,
                height_status="determined" if fid != 3 else "missing",
                representative_height_m=8.0 if fid != 3 else None,
                eave_height_m=7.0,
                ridge_height_m=8.0,
                roof_form="flat",
                evidence_record_path=None,
                evidence_record_sha256=None,
            )
        )
    part = register / "components/part-00000.parquet"
    pq.write_table(pa.Table.from_pylist(rows), part)
    pq.write_table(pa.Table.from_pylist(lineage), register / "source-lineage.parquet")
    (register / "manifest.json").write_text(
        json.dumps(
            {
                "component_count": 4,
                "source_sha256": sha256_file(footprints),
                "partition_sha256": {part.name: sha256_file(part)},
                "source_lineage_sha256": sha256_file(register / "source-lineage.parquet"),
            }
        )
    )
    catalogue = root / "heights.parquet"
    measurement = root / "measurement.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                dict(
                    building_id=rows[0]["building_id"],
                    geometry=rows[0]["geometry"],
                    ground_elevation_m=10.0,
                    quality_flags=["synthetic"],
                )
            ]
        ),
        measurement,
    )
    mapping = root / "height-paths.json"
    mapping.write_text(
        json.dumps({"path_prefixes": [], "evidence_roots": {"immutable_baseline": str(root)}})
    )
    heights[0].update(
        evidence_root="immutable_baseline",
        evidence_record_path=measurement.name,
        evidence_record_sha256=sha256_file(measurement),
        baseline_row_offset=0,
    )
    pq.write_table(pa.Table.from_pylist(heights), catalogue)
    reconciliation = root / "reconciliation.json"
    reconciliation.write_text(
        json.dumps({"status": "publication_blocked", "missing_height_records": 1})
    )
    import sqlite3

    xmlroot = root / "xml"
    xmlroot.mkdir()
    xml = []
    with sqlite3.connect(root / "locator.sqlite") as db:
        db.execute(
            "CREATE TABLE registry_buildings(building_id text PRIMARY KEY,source_file text,source_record integer)"
        )
        for ordinal, bid in enumerate(BUILDINGS, 1):
            xml.append(
                f"<Byggnad><objektidentitet>{bid}</objektidentitet><objektstatus>Gällande</objektstatus><Byggnadsattribut><husnummer>{ordinal}</husnummer><nybyggnadsar>1980</nybyggnadsar></Byggnadsattribut><Bostad><andamal>Flerfamiljshus</andamal><huvudandamal>true</huvudandamal></Bostad><Verksamhet><andamal>Kontor</andamal></Verksamhet><Entre><objektidentitet>e{ordinal}</objektidentitet><adressplats>a{ordinal}</adressplats></Entre></Byggnad>"
            )
            db.execute(
                "INSERT INTO registry_buildings VALUES(?,?,?)", (bid, "buildings.xml", ordinal)
            )
    (xmlroot / "buildings.xml").write_text("<root>" + "".join(xml) + "</root>")
    addresses = root / "addresses"
    addresses.mkdir()
    (addresses / "addresses.xml").write_text(
        '<root xmlns:gml="http://www.opengis.net/gml/3.2"><Belagenhetsadress><objektidentitet>a1</objektidentitet><gml:Point srsName="urn:ogc:def:crs:EPSG::3006"><gml:pos>6400000 320000</gml:pos></gml:Point><faststalltNamn>Testgatan</faststalltNamn></Belagenhetsadress></root>'
    )
    epcroot = root / "epc"
    (epcroot / "fore_v260/data").mkdir(parents=True)
    (epcroot / "fore_v260/data/epc.txt").write_text(
        "FormularId|IdLankod|IdKommunkod|IdFastBet|IdHusnr|EgenAtemp|ExpertGodkannande|EgiEnergiPrestanda\n10|14|85|TOWN 1:1|1|200|2024-01-01 10:00:00|90\n10|14|85|TOWN 1:1|1|200|2024-01-01 10:00:00|90\n10|14|84|ROAD 1:1|3|200|2024-01-01 10:00:00|90\n99|14|84|ROAD 1:1|3|100|2025-01-01 10:00:00|80\n",
        encoding="latin-1",
    )
    paths = {
        "tower_component_register_root": register,
        "lm_building_geometry_primary": footprints,
        "lm_property_geometry": properties,
        "building_registry_locator": root / "locator.sqlite",
        "lm_building_xml_root": xmlroot,
        "lm_address_xml_root": addresses,
        "epc_raw_root": epcroot,
        "height_staging_catalogue": catalogue,
        "height_staging_reconciliation": reconciliation,
    }
    import zipfile

    boundaryroot = root / "boundary"
    boundaryroot.mkdir()
    gpd.GeoDataFrame(
        {"KnKod": ["1485"], "KnNamn": ["Uddevalla"]},
        geometry=[shapely.box(-5, -5, 60, 15)],
        crs=3006,
    ).to_file(boundaryroot / "Kommun_Sweref99TM.shp")
    archive = root / "boundary.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for f in boundaryroot.iterdir():
            z.write(f, f.name)
    from sweden_ubem.scb import POPULATION_FIELDS, acquire_scb_snapshot

    values = {
        "objectid": [1, 2],
        "rutid_inspire": ["i1", "i2"],
        "rutid_scb": ["g1", "g2"],
        "rutstorl": [1000, 1000],
        "referenstid": ["20251231", "20251231"],
    }
    for field in POPULATION_FIELDS:
        values[field] = [10, None]
    population = root / "population.gpkg"
    gpd.GeoDataFrame(
        values, geometry=[shapely.box(0, 0, 10, 10), shapely.box(10, 0, 20, 10)], crs=3006
    ).to_file(population, layer="befolkning_1km_2025")

    def transport(method, url, body, headers):
        doc = (
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "regsokod": "R1",
                            "kommunkod": "1485",
                            "regsonamn": "Fixture",
                        },
                        "geometry": shapely.geometry.mapping(shapely.box(-5, -5, 60, 15)),
                    }
                ],
            }
            if method == "GET"
            else {
                "columns": [
                    {"code": c} for c in ["Region", "InkomstTyp", "Kon", "ContentsCode", "Tid"]
                ],
                "data": [
                    {
                        "key": ["R1_RegSO2025", "SaFörInk", "1+2", "0000089U", "2024"],
                        "values": ["321"],
                    }
                ],
            }
        )
        return json.dumps(doc).encode(), {"content-type": "application/json"}

    snapshot = acquire_scb_snapshot(
        population_grid=population,
        municipality="1485",
        output_root=root / "snapshots",
        transport=transport,
    )
    paths["height_path_mapping"] = mapping
    paths.update(
        scb_municipality_boundaries_primary=archive,
        scb_population_grid_2025=population,
        scb_snapshot_selected=Path(snapshot["path"]),
    )
    return FixtureSources(paths)


def build_height_staging_fixture(root):
    handoff = root / "handoff"
    handoff.mkdir()
    register = root / "register"
    register.mkdir()
    from sweden_ubem.config import REQUIRED_HEIGHT_COLUMNS

    rows = [
        {
            **{key: None for key in REQUIRED_HEIGHT_COLUMNS},
            "component_id": f"b{i}:hash",
            "height_status": s,
        }
        for i, s in enumerate(["determined", "failed", "missing"])
    ]
    catalogue = handoff / "component-height-catalogue.parquet"
    pq.write_table(pa.Table.from_pylist(rows), catalogue)
    (handoff / "reconciliation.json").write_text(
        json.dumps(
            {
                "status": "publication_blocked",
                "component_rows": 3,
                "unique_component_ids": 3,
                "missing_height_records": 1,
                "queue_counts": {"complete": 1, "waiting_source_coverage": 1},
                "unresolved_representation_components": 1,
                "blockers": ["fixture promotion blocker"],
                "sha256": {catalogue.name: sha256_file(catalogue)},
            }
        )
    )
    (register / "manifest.json").write_text(json.dumps({"component_count": 3}))
    pq.write_table(
        pa.Table.from_pylist([{"source_fid": i} for i in range(3)]),
        register / "source-lineage.parquet",
    )
    return catalogue, register
