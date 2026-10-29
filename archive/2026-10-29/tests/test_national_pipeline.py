"""National-once import, DB-only selections, and recovery acceptance."""

import os
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

from fixture_factory import synthetic_sources
from sweden_ubem.database import initialize_database, validate_dataset
from sweden_ubem.ingest import initialize_sources
from sweden_ubem.selection import process_dataset, plan_dataset
from sweden_ubem.explore import UBEM
from sweden_ubem.status import database_status


@pytest.mark.postgis
def test_serial_worker_reuses_connection_and_rolls_back(fresh_url):
    from sweden_ubem.jobs import serial_worker, work

    with psycopg.connect(fresh_url) as c:
        c.execute("INSERT INTO catalog.dataset(dataset_id) VALUES('worker-test')")
    pids = []
    with serial_worker(fresh_url):
        for partition in ("a", "b"):
            with work(fresh_url, "worker-test", "test", partition, "signature") as state:
                pids.append(state["connection"].execute("SELECT pg_backend_pid()").fetchone()[0])
        with pytest.raises(ValueError, match="interruption"):
            with work(fresh_url, "worker-test", "test", "failed", "signature") as state:
                state["connection"].execute(
                    "INSERT INTO catalog.asset(sha256,byte_size) VALUES(%s,1)", ("a" * 64,)
                )
                raise ValueError("interruption")
        with work(fresh_url, "worker-test", "test", "failed", "signature") as state:
            assert (
                state["connection"]
                .execute("SELECT count(*) FROM catalog.asset WHERE sha256=%s", ("a" * 64,))
                .fetchone()[0]
                == 0
            )
            pids.append(state["connection"].execute("SELECT pg_backend_pid()").fetchone()[0])
    assert len(set(pids)) == 1


@pytest.mark.postgis
def test_nested_work_cannot_commit_its_parent(fresh_url):
    from sweden_ubem.jobs import serial_worker, work

    with psycopg.connect(fresh_url) as c:
        c.execute("INSERT INTO catalog.dataset(dataset_id) VALUES('nested-test')")
    with serial_worker(fresh_url):
        with work(fresh_url, "nested-test", "test", "parent", "signature") as parent:
            c = parent["connection"]
            c.execute("CREATE TEMP TABLE parent_workspace(i integer) ON COMMIT DROP")
            parent_pid = c.execute("SELECT pg_backend_pid()").fetchone()[0]
            with work(fresh_url, "nested-test", "test", "child", "signature") as child:
                assert (
                    child["connection"].execute("SELECT pg_backend_pid()").fetchone()[0]
                    != parent_pid
                )
            assert c.execute("SELECT to_regclass('parent_workspace')").fetchone()[0] is not None


def test_arrow_resume_preserves_source_fids(tmp_path):
    from sweden_ubem.ingest import arrow_rows

    s = synthetic_sources(tmp_path / "sources")
    path = s.path("lm_building_geometry_primary")
    full = list(arrow_rows(path, "byggnad"))
    resumed = list(arrow_rows(path, "byggnad", skip_features=2))
    assert [r["_fid"] for r in resumed] == [r["_fid"] for r in full[2:]]
    assert [r["geometry"].wkb for r in resumed] == [r["geometry"].wkb for r in full[2:]]
    assert list(arrow_rows(path, "byggnad", skip_features=len(full))) == []


@pytest.mark.postgis
def test_verified_after_atemp_preserves_conflicts_and_invalid_values(fresh_url):
    from sweden_ubem.epc import observations

    with psycopg.connect(fresh_url) as c:
        cursor = c.execute("SELECT * FROM catalog.field_mapping")
        mappings = [dict(zip([d.name for d in cursor.description], r)) for r in cursor]

    def observation(values):
        rows = [
            {
                "regime": "efter_v260",
                "source_file": "fixture",
                "source_record": str(i),
                "original_values": {"FormularId": "1", "EgenAtemp": v},
            }
            for i, v in enumerate(values)
        ]
        return observations(rows, mappings)[0]

    valid = observation(["354"])
    assert valid["normalized_value"] == {"value": 354.0}
    assert valid["owner_kind"] == "certificate" and valid["normalized_unit"] == "m2"
    assert observation(["354", "355"])["value_status"] == "conflicting"
    assert observation(["Friliggande"])["value_status"] == "failed"
    assert observation(["NaN"])["normalized_value"] is None


@pytest.mark.postgis
def test_completed_file_skips_its_reader(fresh_url, tmp_path, monkeypatch):
    from sweden_ubem.ingest import Importer

    s = sources(tmp_path, monkeypatch)
    initialize_sources(fresh_url, s, layers=["geometry"])
    importer = Importer(fresh_url, s)
    with psycopg.connect(fresh_url) as c:
        importer.version = c.execute(
            "SELECT source_version_id FROM catalog.dataset_layer WHERE dataset_id='national' AND layer='geometry'"
        ).fetchone()[0]
        key = (
            c.execute(
                "SELECT partition_key FROM catalog.work_unit WHERE stage='import:geometry' AND partition_key LIKE '%.parquet:0' LIMIT 1"
            )
            .fetchone()[0]
            .rsplit(":", 1)[0]
        )

    def forbidden(*args):
        raise AssertionError("Completed source file was opened")

    importer.write("geometry", key, forbidden, forbidden, total_rows=4)
    importer.write("geometry", key, forbidden, forbidden)


def test_transport_changes_do_not_change_source_interpretation_digest(monkeypatch):
    from sweden_ubem.ingest import Importer, source_projection_digest

    before = source_projection_digest()

    def transport(self, layer, key, values, transform, *, size=1024, total_rows=None):
        pass

    monkeypatch.setattr(Importer, "write", transport)
    assert source_projection_digest() == before


def test_source_batch_size_change_preserves_checkpoint_contract(monkeypatch):
    from sweden_ubem.ingest import Importer, source_projection_digest

    before = source_projection_digest()

    def changed(self, layer, key, values, transform, *, size=8192, total_rows=None):
        pass

    monkeypatch.setattr(Importer, "write", changed)
    assert source_projection_digest() == before


@pytest.mark.postgis
@pytest.mark.parametrize(
    "changed,accepted", [("collinear", True), ("shifted", False), ("status", False)]
)
def test_parity_geometry_requires_equal_shape_and_unchanged_status(
    fresh_url, tmp_path, changed, accepted
):
    import gzip
    import json
    import shapely
    from scripts.classify_parity import classify

    old = {
        "building_id": "1",
        "geom": shapely.box(0, 0, 10, 10).wkb_hex,
        "area_m2": 100,
        "geometry_status": "resolved",
    }
    new = {**old, "geom": shapely.Polygon([(0, 0), (5, 0), (10, 0), (10, 10), (0, 10)]).wkb_hex}
    if changed == "shifted":
        new["geom"] = shapely.box(1, 0, 11, 10).wkb_hex
    if changed == "status":
        new["geometry_status"] = "ambiguous"
    counts = {"missing_rows": 1, "added_rows": 1, "added_columns": [], "removed_columns": []}
    (tmp_path / "report.json").write_text(
        json.dumps(
            {"equal_functional_values": False, "functional_views": {"building_facts": counts}}
        )
    )
    with gzip.open(tmp_path / "building_facts-differences.jsonl.gz", "wt") as f:
        f.write(json.dumps({"row": old, "baseline_count": 1, "current_count": 0}) + "\n")
        f.write(json.dumps({"row": new, "baseline_count": 0, "current_count": 1}) + "\n")
    assert classify(tmp_path, fresh_url)["accepted"] == accepted


@pytest.fixture
def fresh_url():
    admin = os.environ.get("UBEM_TEST_ADMIN_URL")
    if not admin:
        pytest.fail("UBEM_TEST_ADMIN_URL is required")
    info = conninfo_to_dict(admin)
    info["dbname"] = "postgres"
    admin = make_conninfo(**info)
    name = "ubem_test_national_" + uuid4().hex
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    info["dbname"] = name
    url = make_conninfo(**info)
    try:
        initialize_database(url)
        yield url
    finally:
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(sql.SQL("DROP DATABASE {} WITH(FORCE)").format(sql.Identifier(name)))


def sources(tmp_path, monkeypatch):
    s = synthetic_sources(tmp_path / "sources")
    monkeypatch.setenv("UBEM_SCB_SNAPSHOT", str(s.path("scb_snapshot_selected")))
    return s


@pytest.mark.postgis
def test_process_and_new_release_without_source_files(fresh_url, tmp_path, monkeypatch):
    s = sources(tmp_path, monkeypatch)
    initialize_sources(fresh_url, s)
    import sweden_ubem.ingest as ingest
    import sweden_ubem.dataset as dataset

    def forbidden(*a, **kw):
        raise AssertionError("External source access during processing")

    monkeypatch.setattr(s, "path", forbidden)
    monkeypatch.setattr(ingest, "arrow_rows", forbidden)
    monkeypatch.setattr(ingest, "sha256_file", forbidden)
    monkeypatch.setattr(dataset, "primary_selection", forbidden)
    assert initialize_sources(fresh_url, s)["status"] == "already_imported"
    assert plan_dataset(fresh_url)["primary_buildings"] == 2
    first = process_dataset(fresh_url, assessment_date="2026-10-12")
    assert first["validation"]["valid"]
    assert first["external_source_reads"] == 0
    assert (
        process_dataset(fresh_url, assessment_date="2026-10-12")["layers"]["geometry"]
        == "already_loaded"
    )
    second = process_dataset(fresh_url, dataset="later", assessment_date="2026-10-13")
    assert (
        second["validation"]["valid"]
        and first["processing_release"] != second["processing_release"]
    )
    with psycopg.connect(fresh_url) as c:
        assert c.execute("SELECT count(*) FROM stock.building").fetchone()[0] == 3
        assert c.execute("SELECT count(*) FROM evidence.epc_certificate").fetchone()[0] == 2
        assert (
            c.execute(
                "SELECT count(*) FROM mart.epc_certificates WHERE dataset_id='uddevalla'"
            ).fetchone()[0]
            == 1
        )
    assert len(UBEM(fresh_url).buildings(include_support=True)) == 3
    from sweden_ubem.physical_assessment import assess

    physical = assess(fresh_url)
    assert physical["building_count"] == 3
    assert physical["main"]["gfa_to_atemp"]["count"] == 0
    assert physical["exclusions"]["shared_or_incomplete_certificate_relation"] > 0


@pytest.mark.postgis
def test_neighbor_processing_does_not_expand_certificate_cohort(fresh_url, tmp_path, monkeypatch):
    s = sources(tmp_path, monkeypatch)
    initialize_sources(fresh_url, s)
    process_dataset(
        fresh_url, dataset="neighbor", municipality="1484", assessment_date="2026-10-12"
    )
    first = process_dataset(fresh_url, assessment_date="2026-10-12")
    assert first["validation"]["valid"]
    with psycopg.connect(fresh_url) as c:
        assert c.execute(
            "SELECT formular_id FROM mart.epc_certificates WHERE dataset_id='uddevalla'"
        ).fetchall() == [(10,)]
        assert (
            c.execute(
                "SELECT count(*) FROM mart.epc_subject_results WHERE dataset_id='uddevalla'"
            ).fetchone()[0]
            == 2
        )
        assert (
            c.execute(
                "SELECT count(*) FROM mart.building_epc WHERE dataset_id='uddevalla'"
            ).fetchone()[0]
            == 2
        )
    assert len(UBEM(fresh_url).buildings(include_support=True)) == 3


@pytest.mark.postgis
def test_failed_import_resumes_completed_batches(fresh_url, tmp_path, monkeypatch):
    s = sources(tmp_path, monkeypatch)
    import sweden_ubem.ingest as ingest

    original = ingest.bulk_insert
    failed = False

    def fail_original(c, table, records, **kwargs):
        nonlocal failed
        if table == "raw_lm.record" and not failed:
            failed = True
            raise RuntimeError("interrupted original projection")
        return original(c, table, records, **kwargs)

    monkeypatch.setattr(ingest, "bulk_insert", fail_original)
    with pytest.raises(RuntimeError, match="interrupted"):
        initialize_sources(fresh_url, s)
    status = database_status(fresh_url)
    assert status["sources"] == [{"dataset": "national", "sealed": False}]
    assert status["selections"] == []
    assert status["database_bytes"] > 0
    assert any(
        unit["step"] == "lineage" and unit["state"] == "complete" and unit["source_rows"] > 0
        for unit in status["committed_work"]
    )
    assert any(
        layer["layer"] == "geometry" and layer["status"] == "failed" for layer in status["layers"]
    )
    with psycopg.connect(fresh_url) as c:
        assert c.execute("SELECT count(*) FROM stock.geometry_component").fetchone()[0] == 4
        assert (
            c.execute("SELECT count(*) FROM catalog.work_unit WHERE state='complete'").fetchone()[0]
            > 0
        )
    monkeypatch.setattr(ingest, "bulk_insert", original)
    initialize_sources(fresh_url, s)
    assert process_dataset(fresh_url, assessment_date="2026-10-12")["validation"]["valid"]
    status = database_status(fresh_url)
    assert status["sources"] == [{"dataset": "national", "sealed": True}]
    assert status["selections"] == [
        {
            "dataset": "uddevalla",
            "kind": "municipality_selection",
            "municipality": "1485",
            "sealed": True,
        }
    ]
    with psycopg.connect(fresh_url) as c:
        assert c.execute("SELECT count(*) FROM stock.geometry_component").fetchone()[0] == 4
        assert (
            c.execute("SELECT count(*) FROM raw_lm.record WHERE kind='geometry'").fetchone()[0] == 4
        )
        assert (
            c.execute("SELECT count(*) FROM catalog.work_unit WHERE state='running'").fetchone()[0]
            == 0
        )


@pytest.mark.postgis
def test_late_declared_context_requires_fresh_dependency_signature(
    fresh_url, tmp_path, monkeypatch
):
    s = sources(tmp_path, monkeypatch)
    initialize_sources(
        fresh_url, s, layers=["geometry", "register", "addresses", "properties", "heights", "epc"]
    )
    first = process_dataset(fresh_url, assessment_date="2026-10-12")
    assert not first["validation"]["valid"]
    initialize_sources(fresh_url, s)
    second = process_dataset(fresh_url, assessment_date="2026-10-12")
    assert (
        second["validation"]["valid"]
        and second["processing_release"] != first["processing_release"]
    )
    with psycopg.connect(fresh_url) as c:
        assert set(
            r[0]
            for r in c.execute(
                "SELECT DISTINCT area_type FROM context.building_area WHERE dataset_id='uddevalla'"
            )
        ) == {"municipality", "regso", "population_grid_1km"}
        c.execute(
            "UPDATE catalog.dataset_layer SET input_signature='wrong' WHERE dataset_id='uddevalla' AND layer='context_linkage'"
        )
    assert not validate_dataset(fresh_url, "uddevalla")["valid"]


@pytest.mark.postgis
def test_foreign_snapshot_memberships_and_changed_publication_rejected(
    fresh_url, tmp_path, monkeypatch
):
    s = sources(tmp_path, monkeypatch)
    initialize_sources(fresh_url, s)
    process_dataset(fresh_url, assessment_date="2026-10-12")
    with pytest.raises(ValueError, match="immutable"):
        process_dataset(fresh_url, assessment_date="2026-10-13")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with psycopg.connect(fresh_url) as c:
            c.execute(
                "INSERT INTO scope.dataset_building VALUES('uddevalla','different',%s,'primary')",
                (str(uuid4()),),
            )


@pytest.mark.postgis
def test_national_context_is_not_complete_from_one_snapshot(fresh_url, tmp_path, monkeypatch):
    s = sources(tmp_path, monkeypatch)
    initialize_sources(fresh_url, s)
    result = process_dataset(
        fresh_url, dataset="sweden", municipality=None, assessment_date="2026-10-12"
    )
    assert result["layers"]["regso"] == "not_loaded"
    assert result["layers"]["context_linkage"] == "not_loaded"
    assert not result["validation"]["valid"]
    with psycopg.connect(fresh_url) as c:
        assert not c.execute(
            "SELECT sealed FROM catalog.dataset WHERE dataset_id='sweden'"
        ).fetchone()[0]


@pytest.mark.postgis
def test_changed_source_mapping_requires_reimport(fresh_url, tmp_path, monkeypatch):
    s = sources(tmp_path, monkeypatch)
    initialize_sources(fresh_url, s)
    with psycopg.connect(fresh_url) as c:
        c.execute("UPDATE catalog.field_mapping SET multiplier=2 WHERE source_field='EgenAtemp'")
    with pytest.raises(ValueError, match="field mappings changed"):
        initialize_sources(fresh_url, s)
    with pytest.raises(ValueError, match="field mappings changed"):
        process_dataset(fresh_url)


@pytest.mark.postgis
def test_space_guard_stops_before_batch_and_resumes(fresh_url, tmp_path, monkeypatch):
    s = sources(tmp_path, monkeypatch)
    monkeypatch.setenv("UBEM_CAPACITY_GUARD_PATH", str(tmp_path))
    monkeypatch.setenv("UBEM_MIN_FREE_BYTES", str(10**20))
    with pytest.raises(RuntimeError, match="free-space reserve"):
        initialize_sources(fresh_url, s)
    with psycopg.connect(fresh_url) as c:
        assert c.execute("SELECT count(*) FROM stock.geometry_component").fetchone()[0] == 0
    monkeypatch.setenv("UBEM_MIN_FREE_BYTES", "0")
    initialize_sources(fresh_url, s)
    assert process_dataset(fresh_url, assessment_date="2026-10-12")["validation"]["valid"]


@pytest.mark.postgis
def test_failed_work_is_audited_and_retries_atomically(fresh_url, tmp_path, monkeypatch):
    from sweden_ubem.jobs import work

    s = sources(tmp_path, monkeypatch)
    initialize_sources(fresh_url, s, layers=["geometry"])
    digest = "a" * 64
    with pytest.raises(RuntimeError, match="injected"):
        with work(fresh_url, "national", "import:probe", "one", "signature") as state:
            state["connection"].execute("INSERT INTO catalog.asset(sha256,byte_size) VALUES(%s,1)", (digest,))
            raise RuntimeError("injected failure")
    with psycopg.connect(fresh_url) as c:
        assert not c.execute("SELECT 1 FROM catalog.asset WHERE sha256=%s", (digest,)).fetchone()
        assert (
            c.execute(
                "SELECT status FROM catalog.process_run WHERE layer='import:probe'"
            ).fetchone()[0]
            == "failed"
        )
    with work(fresh_url, "national", "import:probe", "one", "signature") as state:
        state["connection"].execute("INSERT INTO catalog.asset(sha256,byte_size) VALUES(%s,1)", (digest,))
        state["rows"] = 1
    with work(fresh_url, "national", "import:probe", "one", "signature") as state:
        assert state is None
    with psycopg.connect(fresh_url) as c:
        assert c.execute(
            "SELECT generation,state,rows_written FROM catalog.work_unit WHERE stage='import:probe'"
        ).fetchone() == (2, "complete", 1)


@pytest.mark.postgis
def test_partial_source_dependencies_exclude_processing_code(fresh_url, tmp_path, monkeypatch):
    s = sources(tmp_path, monkeypatch)
    initialize_sources(fresh_url, s, layers=["geometry"])
    import sweden_ubem.selection as selection
    import sweden_ubem.ingest as ingest

    monkeypatch.setattr(selection, "application_digest", lambda: "changed-processing-code")
    result = initialize_sources(fresh_url, s, layers=["geometry"])
    assert result["layers"]["geometry"] == "already_imported"

    def changed_reader(*args, **kwargs):
        raise AssertionError("A changed source parser must never run on a partial snapshot")

    monkeypatch.setattr(ingest, "arrow_rows", changed_reader)
    with pytest.raises(ValueError, match="Source projection code changed"):
        initialize_sources(fresh_url, s, layers=["geometry"])


@pytest.mark.postgis
def test_source_diagnostics_preserve_missing_records_and_local_scope(
    fresh_url, tmp_path, monkeypatch
):
    import sqlite3
    import geopandas as gpd
    import shapely
    from fixture_factory import BUILDINGS

    s = sources(tmp_path, monkeypatch)
    with sqlite3.connect(s.path("building_registry_locator")) as locator:
        locator.execute("DELETE FROM registry_buildings WHERE building_id=?", (BUILDINGS[1],))
    path = s.path("lm_property_geometry")
    invalid = gpd.read_file(path, layer="registerenhetsomradesyta").iloc[[0]].copy()
    invalid["objektidentitet"] = "missing-unit"
    invalid["registerenhetsreferens"] = None
    invalid.geometry = [shapely.box(2, 2, 4, 4)]
    invalid.to_file(path, layer="registerenhetsomradesyta", mode="a")
    initialize_sources(fresh_url, s)
    result = process_dataset(fresh_url, assessment_date="2026-10-12")
    assert result["validation"]["valid"]
    with psycopg.connect(fresh_url) as c:
        counts = dict(
            c.execute(
                "SELECT code,count(*) FROM mart.quality_findings WHERE dataset_id='uddevalla' GROUP BY code"
            )
        )
        assert counts["record_missing"] == 1
        assert counts["invalid_property_record"] == 1
        assert counts["unresolved_address"] == 1
        before = c.execute("SELECT count(*) FROM quality.finding").fetchone()[0]
    process_dataset(fresh_url, dataset="same-sources", assessment_date="2026-10-12")
    with psycopg.connect(fresh_url) as c:
        assert before == c.execute("SELECT count(*) FROM quality.finding").fetchone()[0]
