"""Tests create and drop only a unique ubem_test_ database, never operator data."""

import os
import json
from uuid import uuid4
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from fixture_factory import synthetic_sources, BUILDINGS
from sweden_ubem.database import (
    _connect,
    initialize_database,
    create_dataset,
    layer_run,
    insert,
    validate_dataset,
    DatabaseSchemaMismatch,
)
from sweden_ubem.dataset import load_dataset
from sweden_ubem.ingest import initialize_sources
from sweden_ubem.explore import UBEM
from sweden_ubem.exports import export_dataset


@pytest.fixture(scope="module")
def test_url():
    admin = os.environ.get("UBEM_TEST_ADMIN_URL")
    if not admin:
        pytest.fail("UBEM_TEST_ADMIN_URL is required for PostGIS integration; no silent skip")
    info = conninfo_to_dict(admin)
    info["dbname"] = "postgres"
    admin = make_conninfo(**info)
    name = "ubem_test_" + uuid4().hex
    with _connect(admin) as c:
        c.autocommit = True
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    info["dbname"] = name
    url = make_conninfo(**info)
    try:
        initialize_database(url)
        yield url
    finally:
        with _connect(admin) as c:
            c.autocommit = True
            c.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.mark.postgis
def test_independent_layers_and_exploration(test_url, tmp_path, monkeypatch):
    s = synthetic_sources(tmp_path / "sources")
    monkeypatch.setenv("UBEM_SCB_SNAPSHOT", str(s.path("scb_snapshot_selected")))
    initialize_sources(test_url, s)
    result = load_dataset(test_url)
    assert all(x == "loaded" for x in result["layers"].values()), json.dumps(
        result, indent=2, default=str
    )
    db = UBEM(test_url)
    assert len(db.buildings()) == 2
    assert len(db.buildings(include_support=True)) == 3
    assert len(db.buildings(purpose="Verksamhet")) == 2
    b = db.building(BUILDINGS[0])
    assert len(b.components()) == 2
    assert len(b.heights()) == 2 and len(b.epc_certificates()) == 1
    with _connect(test_url) as c:
        assert c.execute(
            "SELECT ST_Equals(m.geom,r.geom) FROM mart.context_areas m CROSS JOIN (SELECT ST_Union(geom) geom FROM mart.context_areas WHERE dataset_id='uddevalla' AND area_type='regso') r WHERE m.dataset_id='uddevalla' AND m.area_type='municipality'"
        ).fetchone()[0]
        assert (
            c.execute(
                "SELECT count(*) FROM raw_scb.record WHERE dataset_id='national' AND kind='regso'"
            ).fetchone()[0]
            == 1
        )
        assert c.execute(
            "SELECT linkage_geom IS NOT NULL FROM stock.building_footprint WHERE dataset_id='national' AND building_id=%s",
            (BUILDINGS[1],),
        ).fetchone()[0]
        assert (
            c.execute("SELECT count(*) FROM raw_height.record WHERE kind='measurement'").fetchone()[
                0
            ]
            == 1
        )
    assert b.epc_certificates().iloc[0].certificate_date.year == 2024
    assert len(b.addresses()) == 1
    assert len(db.areas("regso")) == 1
    assert len(db.population_grid()) == 2
    assert len(b.context_summary()) == 1
    assert validate_dataset(test_url, "uddevalla")["valid"]
    from sweden_ubem.comparison import compare_oracle
    import pyarrow as pa
    import pyarrow.parquet as pq

    oracle = tmp_path / "oracle" / "candidate_links" / "kommunkod=1485"
    oracle.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist(
            [
                dict(
                    building_id=BUILDINGS[0],
                    formular_id=10,
                    epc_municipality_code="1485",
                    epc_property_normalized="TEST 1:1",
                    epc_house_number=1,
                    decision="accepted",
                    rejection_reason=None,
                )
            ]
        ),
        oracle / "part.parquet",
    )
    report = compare_oracle(
        test_url, "uddevalla", tmp_path / "oracle", tmp_path / "comparison.json"
    )
    assert report["report_path"] == str(tmp_path / "comparison.json")
    with _connect(test_url) as c:
        assert c.execute(
            "SELECT count(*),sum(CASE WHEN selection_role='primary' THEN 1 ELSE 0 END) FROM mart.building_facts_current"
        ).fetchone() == (3, 2)
        assert (
            c.execute(
                "SELECT sum(overlap_fraction) FROM evidence.building_property WHERE building_id=%s",
                (BUILDINGS[0],),
            ).fetchone()[0]
            == 1
        )
        assert (
            c.execute(
                "SELECT count(*) FROM mart.epc_certificates WHERE dataset_id='uddevalla' AND formular_id=99"
            ).fetchone()[0]
            == 0
        )
        assert c.execute("SELECT count(*) FROM mart.building_epc").fetchone()[0] == 2
        assert (
            c.execute(
                "SELECT normalized_value FROM context.observation WHERE indicator_id='scb:income:median_earned'"
            ).fetchone()[0]
            == 321000
        )
    for format in ("csv", "geoparquet", "geopackage"):
        result = export_dataset(
            test_url, "uddevalla", [BUILDINGS[0]], format=format, output=tmp_path / format
        )
        assert result["tables"]["epc_certificates"] == 1
        assert result["tables"]["building_components"] == 2
    from sweden_ubem.scopes import evaluate_scopes

    evaluate_scopes(
        test_url, "uddevalla", "mixed", {"purposes": ["Verksamhet"], "include_support": False}
    )
    with _connect(test_url) as c:
        assert (
            c.execute(
                "SELECT count(*) FROM scope.membership WHERE scope_id='mixed' AND inclusion_state='included'"
            ).fetchone()[0]
            == 2
        )
    import nbformat
    from nbclient import NotebookClient

    monkeypatch.setenv("UBEM_DATABASE_URL", test_url)
    notebook = nbformat.read("notebooks/explore_uddevalla.ipynb", as_version=4)
    NotebookClient(notebook, timeout=60, kernel_name="python3").execute()


@pytest.mark.postgis
def test_transaction_failure_and_hash_constraint(test_url):
    create_dataset(test_url, "failure", "1485")
    with pytest.raises(ValueError, match="deliberate"):
        with layer_run(test_url, "failure", "geometry", {}) as (c, run):
            insert(
                c,
                "stock.building",
                dict(
                    dataset_id="failure",
                    building_id=BUILDINGS[0],
                    municipality_code="1485",
                ),
            )
            raise ValueError("deliberate")
    with _connect(test_url) as c:
        assert (
            c.execute("SELECT count(*) FROM stock.building WHERE dataset_id='failure'").fetchone()[
                0
            ]
            == 0
        )
        assert (
            c.execute(
                "SELECT loading_status FROM catalog.dataset_layer WHERE dataset_id='failure' AND layer='geometry'"
            ).fetchone()[0]
            == "failed"
        )
    assert initialize_database(test_url) == []
    create_dataset(test_url, "interrupted", "1485")
    with pytest.raises(KeyboardInterrupt):
        with layer_run(test_url, "interrupted", "geometry", {}):
            raise KeyboardInterrupt("operator interruption")
    with _connect(test_url) as c:
        assert (
            c.execute(
                "SELECT loading_status FROM catalog.dataset_layer WHERE dataset_id='interrupted' AND layer='geometry'"
            ).fetchone()[0]
            == "failed"
        )


@pytest.mark.postgis
def test_checksums_permissions_and_invalid_height_identity(test_url, tmp_path):
    import shutil
    import psycopg
    from sweden_ubem.database import MIGRATIONS

    copied = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS, copied)
    path = copied / "001_compact.sql"
    path.write_text(path.read_text() + "\n-- changed\n")
    with pytest.raises(DatabaseSchemaMismatch):
        initialize_database(test_url, copied)
    with _connect(test_url) as c:
        c.execute("SET LOCAL ROLE ubem_readonly")
        assert c.execute("SELECT count(*) FROM mart.building_facts").fetchone()[0] == 3
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT count(*) FROM raw_lm.record")
    with _connect(test_url) as c:
        component, run = c.execute(
            "SELECT component_id,run_id FROM stock.geometry_component LIMIT 1"
        ).fetchone()
    from sweden_ubem.jobs import bulk_insert
    with pytest.raises(ValueError, match="geometry hash"):
        with _connect(test_url) as c:
            bulk_insert(c,"evidence.component_height",[dict(dataset_id="national",component_id=component,
                input_geometry_hash="f"*64,height_status="missing",representative_height_m=None,
                eave_height_m=None,ridge_height_m=None,roof_form=None,details={},evidence_sha256=None,run_id=run)])
    # A hash-consistent locator still requires an existing component parent.
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with _connect(test_url) as c:
            c.execute(
                "UPDATE evidence.component_height_data SET component_key=-1 WHERE component_key=(SELECT min(component_key) FROM evidence.component_height_data)",
            )
    with _connect(test_url) as c:
        assert not c.execute("SELECT 1 FROM pg_attribute WHERE attrelid='evidence.component_height_data'::regclass AND attname IN ('component_id','input_geometry_hash') AND NOT attisdropped").fetchall()


@pytest.mark.postgis
def test_notebook_navigation_uses_readonly_views(test_url, monkeypatch):
    import sweden_ubem.explore as explore

    def readonly(url):
        c = _connect(url)
        c.execute("SET ROLE ubem_readonly")
        return c

    monkeypatch.setattr(explore, "_connect", readonly)
    db = explore.UBEM(test_url)
    assert len(db.buildings(purpose="Bostad", scope="all-buildings")) == 2
    b = db.building(BUILDINGS[0])
    assert len(b.purposes()) == 2
    assert not b.epc_certificates().empty


@pytest.mark.postgis
def test_rebuild_refuses_unconfirmed_target_and_external_dependents(test_url, tmp_path):
    from sweden_ubem.database import rebuild_database

    with pytest.raises(ValueError, match="Explicit"):
        rebuild_database("not-a-url", expected_database="x", backup=tmp_path / "no.dump")
    with pytest.raises(ValueError, match="target"):
        rebuild_database(
            test_url, expected_database="different", backup=tmp_path / "no.dump", confirmed=True
        )
    with _connect(test_url) as c:
        c.execute("CREATE VIEW public.outside_dependency AS SELECT building_id FROM stock.building")
    try:
        with pytest.raises(ValueError, match="External dependencies"):
            rebuild_database(
                test_url,
                expected_database=conninfo_to_dict(test_url)["dbname"],
                backup=tmp_path / "no.dump",
                confirmed=True,
            )
        assert not (tmp_path / "no.dump").exists()
    finally:
        with _connect(test_url) as c:
            c.execute("DROP VIEW public.outside_dependency")


@pytest.mark.postgis
def test_backed_up_rebuild_and_restore(test_url, tmp_path):
    import subprocess
    from sweden_ubem.database import rebuild_database

    container = os.environ.get("UBEM_TEST_BACKUP_CONTAINER", "sweden_building_combined-postgis-1")
    info = conninfo_to_dict(test_url)
    info["dbname"] = "postgres"
    admin = make_conninfo(**info)
    names = ["ubem_test_rebuild_" + uuid4().hex, "ubem_test_restore_" + uuid4().hex]
    with _connect(admin) as c:
        c.autocommit = True
        for name in names:
            c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        info["dbname"] = names[0]
        url = make_conninfo(**info)
        initialize_database(url)
        create_dataset(url, "backup", "1485")
        with _connect(url) as c:
            insert(
                c,
                "stock.building",
                dict(
                    dataset_id="backup",
                    building_id=BUILDINGS[0],
                    municipality_code="1485",
                ),
            )
        backup = tmp_path / "before.dump"
        rebuild_database(
            url,
            expected_database=names[0],
            backup=backup,
            confirmed=True,
            backup_container=container,
        )
        assert backup.stat().st_size > 0
        with _connect(url) as c:
            assert c.execute("SELECT count(*) FROM stock.building").fetchone()[0] == 0
        restore_command = (
            [
                "docker",
                "exec",
                "-i",
                container,
                "pg_restore",
                "--no-owner",
                "-U",
                info["user"],
                "-d",
                names[1],
            ]
            if container
            else [
                os.environ.get("UBEM_TEST_PG_RESTORE", "pg_restore"),
                "--no-owner",
                "--dbname",
                make_conninfo(**{**info, "dbname": names[1]}),
            ]
        )
        subprocess.run(
            restore_command,
            input=backup.read_bytes(),
            check=True,
            capture_output=True,
        )
        info["dbname"] = names[1]
        with _connect(make_conninfo(**info)) as c:
            assert c.execute("SELECT count(*) FROM stock.building").fetchone()[0] == 1
    finally:
        with _connect(admin) as c:
            c.autocommit = True
            for name in names:
                c.execute(sql.SQL("DROP DATABASE {} WITH(FORCE)").format(sql.Identifier(name)))


@pytest.mark.postgis
def test_unmatched_outcomes_and_readonly_reporting_exports(test_url, tmp_path, monkeypatch):
    from sweden_ubem.epc_reporting import epc_report
    import sweden_ubem.exports as exports
    import sweden_ubem.epc_reporting as reporting

    s = synthetic_sources(tmp_path / "unmatched-sources")
    path = s.path("epc_raw_root") / "fore_v260/data/epc.txt"
    with path.open("a") as stream:
        stream.write("20|14|85|MISSING 1:9|77|450|2020-01-01 10:00:00|120\n")
        stream.write("10|14|85|MISSING 1:9|78|200|2024-01-01 10:00:00|90\n")
    monkeypatch.setenv("UBEM_SCB_SNAPSHOT", str(s.path("scb_snapshot_selected")))
    from sweden_ubem.hashing import sha256_file

    document = s.path("epc_raw_root").parent / "site-source.pdf"
    document.write_bytes(b"%PDF-synthetic-test-source")
    event_path = s.path("epc_raw_root").parent / "site-events.json"
    event_path.write_text(
        json.dumps(
            dict(
                contract="epc-site-event-v1",
                events=[
                    dict(
                        municipality_code="1485",
                        property_match_key="1485|TOWN 1:1",
                        event_kind="replacement",
                        reported_date="2025-07-01",
                        date_basis="opening_date",
                        description="Synthetic site change.",
                        source_url="https://example.org/site.pdf",
                        evidence_scope="site",
                        source_document=document.name,
                        source_document_sha256=sha256_file(document),
                    )
                ],
            )
        )
    )
    initialize_sources(
        test_url, s, source="unmatched-source", bundle={"site_event_files": [str(event_path)]}
    )
    result = load_dataset(
        test_url, dataset="unmatched", source="unmatched-source", assessment_date="2026-10-12"
    )
    assert all(x == "loaded" for x in result["layers"].values()), result
    assert validate_dataset(test_url, "unmatched")["valid"]
    with _connect(test_url) as c:
        assert (
            c.execute(
                "SELECT count(*) FROM mart.epc_subject_results WHERE dataset_id='unmatched'"
            ).fetchone()[0]
            == 4
        )
        assert (
            c.execute(
                "SELECT count(*) FROM mart.epc_subject_results WHERE dataset_id='unmatched' AND outcome='no_candidate'"
            ).fetchone()[0]
            == 2
        )
        assert dict(
            c.execute(
                "SELECT formular_id,matching_status FROM mart.epc_certificates WHERE dataset_id='unmatched'"
            )
        ) == {10: "partly_matched", 20: "unmatched"}
        assert (
            c.execute(
                "SELECT dataset_id FROM catalog.active_dataset WHERE municipality_code='1485'"
            ).fetchone()[0]
            == "unmatched"
        )
        assert (
            c.execute(
                "SELECT count(*) FROM mart.epc_subject_results WHERE dataset_id='uddevalla'"
            ).fetchone()[0]
            == 2
        )

    def readonly(url):
        c = _connect(url)
        c.execute("SET ROLE ubem_readonly")
        return c

    monkeypatch.setattr(exports, "_connect", readonly)
    monkeypatch.setattr(reporting, "_connect", readonly)
    report = epc_report(test_url, "unmatched", tmp_path / "diagnostics")
    assert report["subjects"]["no_candidate"] == 2
    source_rows = json.loads((tmp_path / "diagnostics/source_inventory.json").read_text())
    site_rows = [r for r in source_rows if r["layer"] == "epc_site_events"]
    assert len(site_rows) == 2 and all(
        r["publisher"] == "published site evidence" for r in site_rows
    )
    assert {r["sha256"] for r in site_rows} == {sha256_file(document), sha256_file(event_path)}
    assert (
        report["certificates"]["partly_matched"] == 1 and report["certificates"]["unmatched"] == 1
    )
    exported = export_dataset(
        test_url, "unmatched", [BUILDINGS[0]], format="csv", output=tmp_path / "readonly-export"
    )
    assert exported["tables"]["epc_subject_results"] == 3


@pytest.mark.postgis
def test_completion_clock_and_immutable_epc_decisions(test_url):
    import psycopg

    create_dataset(test_url, "clock", "1485")
    with layer_run(test_url, "clock", "geometry", {}) as (c, run):
        c.execute("SELECT pg_sleep(0.03)")
        observed = c.execute("SELECT clock_timestamp()").fetchone()[0]
    with _connect(test_url) as c:
        started, finished = c.execute(
            "SELECT started_at,finished_at FROM catalog.process_run WHERE run_id=%s", (run,)
        ).fetchone()
        assert finished >= observed and (finished - started).total_seconds() >= 0.03
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        with _connect(test_url) as c:
            c.execute(
                "UPDATE evidence.epc_subject_result SET outcome='no_candidate' WHERE dataset_id='national'"
            )
