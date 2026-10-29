"""Acceptance on an explicitly provisioned, disposable GIS lake copy only."""

import json
import os

import geopandas as gpd
import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
import pytest

from epsm_db import Database, Lake, PublicationPendingError
from epsm_db.errors import BoundaryUnavailableError, RecordNotFoundError

pytestmark = pytest.mark.postgis


@pytest.fixture(scope="module")
def lake():
    url = os.environ.get("EPSM_LAKE_TEST_URL")
    if not url:
        pytest.skip("Set EPSM_LAKE_TEST_URL to a disposable database copy")
    with psycopg.connect(url, autocommit=True) as c:
        assert c.info.dbname.startswith("epsm_api_validation_"), (
            "Refusing to mutate a non-validation database"
        )
        version = c.execute("SELECT version FROM lake.schema_version").fetchone()[0]
    lake = Lake(url)
    if version == 1:
        assert lake.migrate()["changed"]
    assert not lake.migrate()["changed"]
    with pytest.raises(PublicationPendingError):
        len(Database(url).stock())
    with lake.connect(autocommit=True) as c:
        source = c.execute(
            "INSERT INTO lake.source(name,kind,version) VALUES('api-acceptance','mixed','1') RETURNING source_id"
        ).fetchone()[0]
        file = c.execute(
            "INSERT INTO lake.import_file(source_id,path,byte_size,mtime_ns,state) VALUES(%s,'synthetic-evidence',0,0,'complete') RETURNING file_id",
            (source,),
        ).fetchone()[0]
        identifiers = [
            "a",
            "b",
            "missing",
            "shared-a",
            "shared-b",
            "tied",
            "unknown",
            "conflict",
            "invalid",
        ]
        keys = {}
        for i, name in enumerate(identifiers):
            purpose = [{"category": "Bostad", "detail": "Flerfamiljshus"}]
            keys[name] = c.execute(
                """INSERT INTO lake.building(external_id,municipality_code,construction_year,purposes,geom,geometry_valid)
                VALUES(%s,'1485',%s,%s,CASE WHEN %s THEN NULL ELSE ST_Multi(ST_MakeEnvelope(%s,6500000,%s,6500010,3006)) END,true)
                RETURNING building_id""",
                (
                    "api-" + name,
                    None if name == "missing" else 1965,
                    Jsonb(purpose),
                    name == "missing",
                    500000 + i * 30,
                    500010 + i * 30,
                ),
            ).fetchone()[0]
        for name in ["a", "b"]:
            for part in range(2 if name == "a" else 1):
                cid = c.execute(
                    """INSERT INTO lake.component(external_id,building_id,geom,geometry_valid,file_id,source_record)
                    VALUES(%s,%s,ST_Multi(ST_MakeEnvelope(%s,6500000,%s,6500010,3006)),true,%s,%s) RETURNING component_id""",
                    (
                        "api-" + name + "-" + str(part),
                        keys[name],
                        500000 + part * 5,
                        500005 + part * 5,
                        file,
                        "component-" + name + str(part),
                    ),
                ).fetchone()[0]
                for measured, height in [
                    ("2025-01-01", 6 + part * 3),
                    ("2026-01-01", 7 + part * 3),
                ]:
                    c.execute(
                        """INSERT INTO lake.height(component_id,component_external_id,height_status,representative_height_m,file_id,source_record,measured_at)
                        VALUES(%s,%s,'determined',%s,%s,%s,%s)""",
                        (
                            cid,
                            "api-" + name + "-" + str(part),
                            height,
                            file,
                            f"height-{name}-{part}-{measured}",
                            measured,
                        ),
                    )
        c.execute(
            """INSERT INTO lake.property_part(external_id,municipality_code,geom,file_id,source_record,attributes)
            VALUES('api-unresolved','1485',ST_Multi(ST_MakeEnvelope(499999,6499999,500011,6500011,3006)),%s,'unresolved',%s)""",
            (file, Jsonb({"objekttyp": "outrett fastighetsområde"})),
        )

        def certificate(number, names, approval="2025-01-01", extra=None):
            cert = c.execute(
                "INSERT INTO lake.epc(external_id,regime,certificate_date) VALUES(%s,'efter_v260','2025-01-01') RETURNING epc_id",
                (f"efter_v260:{number}",),
            ).fetchone()[0]
            raw = {
                "FormularId": str(number),
                "ExpertGodkannande": approval,
                "EgenAtemp": "100",
                "EgenAntalPlan": "2",
                "VentTypF": "Ja",
            }
            if extra:
                raw.update(extra)
            for j, name in enumerate(names):
                sub = c.execute(
                    """INSERT INTO lake.epc_subject(epc_id,building_external_id) VALUES(%s,%s) RETURNING subject_id""",
                    (cert, "api-" + name),
                ).fetchone()[0]
                c.execute(
                    "INSERT INTO lake.building_epc(building_id,epc_id,subject_id,status,method) VALUES(%s,%s,%s,'matched','source_building_reference')",
                    (keys[name], cert, sub),
                )
            c.execute(
                "INSERT INTO lake.epc_record(epc_id,file_id,source_record,attributes) VALUES(%s,%s,%s,%s)",
                (cert, file, "epc-" + str(number), Jsonb(raw)),
            )
            return cert, raw

        certificate(99001, ["a"])
        certificate(99002, ["shared-a", "shared-b"])
        certificate(99003, ["tied"])
        certificate(99004, ["tied"])
        certificate(99005, ["unknown"])
        certificate(99006, ["unknown"], approval="not a date")
        conflict, raw = certificate(99007, ["conflict"])
        c.execute(
            "INSERT INTO lake.epc_record(epc_id,file_id,source_record,attributes) VALUES(%s,%s,%s,%s)",
            (conflict, file, "conflicting-area", Jsonb(dict(raw, EgenAtemp="200"))),
        )
        certificate(99008, ["invalid"], extra={"EgenAtemp": "NaN", "EgenAntalPlan": "2.5"})
        area = c.execute(
            """INSERT INTO lake.area(area_type,external_id,boundary_version,name,geom,geometry_valid,file_id,source_record)
            VALUES('deso','api-area','2025','Test area',ST_Multi(ST_MakeEnvelope(499990,6499990,500500,6500020,3006)),true,%s,'area') RETURNING area_id""",
            (file,),
        ).fetchone()[0]
        for key in keys.values():
            c.execute(
                "INSERT INTO lake.building_area(building_id,area_id,status) VALUES(%s,%s,'matched')",
                (key, area),
            )
        c.execute(
            """INSERT INTO lake.statistic(area_type,area_external_id,boundary_version,area_id,indicator,period,unit,value,value_status,source_field,file_id,source_record)
            VALUES('deso','api-area','2025',%s,'test_population','2024','persons',1000,'reported','count',%s,'statistic')""",
            (area, file),
        )
        c.execute("UPDATE lake.publication SET revision=2,linked_revision=2")
    lake.publish()
    yield lake


def chosen(lake):
    return Database(lake.url).stock(bbox=(499990, 6499990, 500500, 6500020))


def test_count_frame_iterator_and_complete_scoped_extract_agree(lake, tmp_path):
    stock = chosen(lake)
    ids = {r.building_id for r in stock.iter_records(batch_size=2)}
    assert len(stock) == len(ids) == 8
    assert set(stock.frame().building_id) == ids
    cell = stock.cell(building_type="multi_family", era="SE.02")
    assert len(cell) == len(ids)
    output = tmp_path / "selection.gpkg"
    metadata = cell.extract(output, batch_rows=2)
    exported = gpd.read_file(output, layer="buildings")
    assert set(exported.building_external_id) == ids
    assert metadata["layers"]["buildings"] == len(ids)
    assert (
        json.loads(output.with_suffix(".metadata.json").read_text())["selection"]["stock"]["era"]
        == "SE.02"
    )
    with pytest.raises(FileExistsError):
        cell.extract(output)


def test_missing_geometry_remains_in_stock_and_stock_extract(lake, tmp_path):
    stock = Database(lake.url).stock(municipality="1485")
    records = list(stock)
    assert any(r.building_id == "api-missing" and r.geometry is None for r in records)
    output = tmp_path / "whole-stock.gpkg"
    stock.extract(output, layers=["buildings"])
    assert set(gpd.read_file(output).building_external_id) == {r.building_id for r in records}


def test_all_height_evidence_and_unresolved_property_parts(lake):
    r = chosen(lake).record("api-a")
    assert len(r.components) == 2 and len(r.heights) == 4
    assert {h["component_id"] for h in r.heights} == {c["component_id"] for c in r.components}
    assert not any("geometry_hash" in h for h in r.heights)
    assert r.property_parts[0]["property_id"] is None
    assert r.property_parts[0]["relationship"] == "unresolved_spatial_candidate"
    assert len(chosen(lake).component_frame()) == 3


def test_conservative_epc_scope_dates_and_conflicts(lake):
    stock = chosen(lake)
    assert stock.record("api-a").single_building_epc
    assert stock.record("api-a").atemp_m2 == 100
    assert stock.record("api-a").storeys == 2
    assert not stock.record("api-shared-a").single_building_epc
    assert stock.record("api-shared-a").atemp_m2 is None
    assert stock.record("api-tied").selected_certificate is None
    assert stock.record("api-unknown").selected_certificate is None
    assert stock.record("api-conflict").atemp_m2 is None
    assert stock.record("api-invalid").atemp_m2 is None
    assert stock.record("api-invalid").storeys is None
    counts = stock.ventilation_counts()
    assert counts.buildings.sum() == len(stock)
    assert counts.loc["F", "buildings"] == 3


def test_bounded_preview_cursors_and_empty_geodataframes(lake):
    stock = chosen(lake)
    with pytest.warns(UserWarning, match="Preview limited"):
        first = stock.frame(limit=2)
    receipt = first.attrs["receipt"]
    assert receipt.truncated
    with pytest.warns(UserWarning):
        second = stock.frame(limit=2, after=receipt.next_cursor)
    assert not set(first.building_id) & set(second.building_id)
    empty = Database(lake.url).stock(bbox=(0, 0, 1, 1)).frame()
    assert empty.empty and empty.crs.to_epsg() == 3006
    with pytest.raises(RecordNotFoundError):
        stock.record("not-present")


def test_matrix_totals_and_cell_filter_match(lake):
    stock = chosen(lake)
    matrix = stock.matrix()
    assert matrix.loc["TOTAL", "Total"] == len(stock)
    assert matrix.loc["multi_family", "SE.02"] == len(stock)
    vent = stock.cell(building_type="multi_family", era="SE.02", ventilation="F")
    assert len(vent) == 3
    assert {r.building_id for r in vent} == set(vent.frame().building_id)


def test_current_epsm_cell_conversion_and_application(lake):
    pytest.importorskip("epsm")
    cell = chosen(lake).cell(building_type="multi_family", era="SE.02", ventilation="F")
    models, receipts = cell.to_epsm(storeys=2, storey_height_m=3)
    assert len(models) == len(receipts) == len(cell)
    assert all(m.construction_set is None for m in models)
    models, receipts = cell.to_epsm(
        archetype="cell", calendar={"calendar_year": 2023}, storeys=2, storey_height_m=3
    )
    assert all(m.construction_set is not None and m.archetype_applications for m in models)
    assert all(m.to_dict()["source_record"]["portable_record"] for m in models)
    assert all(not r.ready_for_simulation for r in receipts)


def test_statistics_require_explicit_period_and_remain_area_context(lake):
    handle = Database(lake.url).building("api-a")
    assert handle.areas().iloc[0].boundary_version == "2025"
    observations = handle.context(indicator="test_population", period="2024")
    assert observations.iloc[0].value == 1000
    assert observations.iloc[0].interpretation == "area_context"


def test_boundary_editions_are_explicit_and_exact(lake):
    stock = Database(lake.url).stock(
        area_type="deso", area_code="api-area", boundary_version="2025"
    )
    assert len(stock) == 8
    with pytest.raises(BoundaryUnavailableError):
        len(Database(lake.url).stock(area_type="deso", area_code="missing"))


def test_publication_fence_and_reader_permissions(lake):
    admin_url = os.environ.get("EPSM_LAKE_TEST_ADMIN_URL")
    if not admin_url:
        pytest.skip("Reader permission validation needs the disposable database administrator")
    lake.grant_reader("ubem_readonly")
    reader = Database(make_conninfo(admin_url, options="-c role=ubem_readonly"))
    assert reader.record("api-a").building_id == "api-a"
    with reader._lake.connect() as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT * FROM lake.building LIMIT 1")
    with lake.connect(autocommit=True) as c:
        c.execute("UPDATE lake.publication SET revision=revision+1")
    try:
        with pytest.raises(PublicationPendingError):
            len(reader.stock())
        assert reader.features("buildings", limit=1)
    finally:
        with lake.connect(autocommit=True) as c:
            c.execute("UPDATE lake.publication SET revision=revision-1")


def test_hydration_query_count_does_not_grow_per_building(lake, monkeypatch):
    statements = []
    original = psycopg.Connection.execute

    def tracked(connection, query, *args, **kwargs):
        statements.append(
            query.as_string(connection) if hasattr(query, "as_string") else str(query)
        )
        return original(connection, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "execute", tracked)
    stock = chosen(lake)
    assert len(list(stock.iter_records(batch_size=100))) == 8
    many = len(statements)
    statements.clear()
    assert stock.record("api-a")
    assert many == len(statements) + 1  # Full iteration performs one final empty keyset page.
    assert many <= 16
    assert not any(" OFFSET " in statement.upper() for statement in statements)


def test_qgis_map_metadata_and_portable_source_keys(lake, tmp_path):
    with lake.connect() as c:
        for layer in [
            "buildings",
            "component_heights",
            "properties",
            "addresses",
            "epc_links",
            "boundaries",
            "roads",
        ]:
            result = c.execute(
                """SELECT type,srid FROM geometry_columns WHERE f_table_schema='gis' AND f_table_name=%s""",
                (layer,),
            ).fetchone()
            assert result and result[1] == 3006
            assert c.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE schemaname='gis' AND tablename=%s AND indexdef LIKE 'CREATE UNIQUE INDEX%%')",
                (layer,),
            ).fetchone()[0]
    output = tmp_path / "ids.gpkg"
    chosen(lake).extract(output, batch_rows=1)
    frame = gpd.read_file(output, layer="buildings")
    assert frame.fid.is_unique and frame.fid.dtype.kind in "iu"
    assert frame.crs.to_epsg() == 3006


def test_real_sample_evidence_is_readable_after_migration(lake):
    stock = Database(lake.url).stock()
    records = tuple(stock.iter_records(batch_size=3))
    real = [r for r in records if not r.building_id.startswith("api-")]
    assert real
    assert len(records) == len(stock)
    assert any(r.components for r in real)
    assert all(
        r.receipt.snapshot["model_revision"] == r.receipt.snapshot["source_revision"] for r in real
    )
