import json
from pathlib import Path
import pytest
import psycopg

from test_national_pipeline import fresh_url, sources
from sweden_ubem.ingest import initialize_sources
from sweden_ubem.source_inspection import inspect_record
from sweden_ubem.status import database_status


@pytest.mark.postgis
def test_resume_after_changing_both_batch_limits(fresh_url,tmp_path,monkeypatch):
    import sweden_ubem.ingest as ingest
    fixture = sources(tmp_path,monkeypatch)
    original = ingest.bulk_insert
    failed = False
    def interrupt(c,table,records,**kwargs):
        nonlocal failed
        if table == "raw_lm.record" and records and records[0]["kind"] == "geometry" and int(records[0]["source_record"]) >= 3 and not failed:
            failed = True
            raise RuntimeError("interrupt after committed records")
        return original(c,table,records,**kwargs)
    monkeypatch.setattr(ingest,"bulk_insert",interrupt)
    with pytest.raises(RuntimeError,match="committed records"):
        initialize_sources(fresh_url,fixture,layers=["geometry"],batch_rows=2,batch_bytes=4096)
    with psycopg.connect(fresh_url) as c:
        assert c.execute("SELECT count(*) FROM raw_lm.record WHERE kind='geometry'").fetchone()[0] == 2
    monkeypatch.setattr(ingest,"bulk_insert",original)
    initialize_sources(fresh_url,fixture,layers=["geometry"],batch_rows=3,batch_bytes=8192)
    with psycopg.connect(fresh_url) as c:
        assert c.execute("SELECT count(*) FROM stock.component_data").fetchone()[0] == 4
        assert c.execute("SELECT count(*) FROM raw_lm.record WHERE kind='geometry'").fetchone()[0] == 4
    status=database_status(fresh_url)
    assert sum(row["source_rows"] or 0 for row in status["committed_work"] if row["state"] == "complete") == 12


@pytest.mark.postgis
def test_original_inspection_after_root_move_missing_and_changed_asset(fresh_url,tmp_path,monkeypatch):
    fixture=sources(tmp_path,monkeypatch)
    initialize_sources(fresh_url,fixture,layers=["geometry"])
    with psycopg.connect(fresh_url) as c:
        record_id=str(c.execute("SELECT record_id FROM mart.source_record_locators WHERE kind='geometry' ORDER BY record_id LIMIT 1").fetchone()[0])
    root=fixture.path("lm_building_geometry_primary").parent
    original=inspect_record(fresh_url,record_id,root)
    new_root=root.with_name("moved-sources")
    root.rename(new_root)
    assert not root.exists()
    assert inspect_record(fresh_url,record_id,new_root)["original_record"] == original["original_record"]
    asset=new_root/original["bundle_path"]
    with asset.open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError,match="checksum changed"):
        inspect_record(fresh_url,record_id,new_root)
    asset.unlink()
    with pytest.raises(FileNotFoundError,match="missing"):
        inspect_record(fresh_url,record_id,new_root)


@pytest.mark.postgis
def test_staging_reused_across_commits_and_recovered_after_rollback(fresh_url):
    from sweden_ubem.jobs import serial_worker,work,bulk_insert
    with psycopg.connect(fresh_url) as c:
        c.execute("INSERT INTO catalog.dataset(dataset_id) VALUES('staging-test')")
    def row(n):
        return dict(dataset_id="staging-test",building_id=f"00000000-0000-0000-0000-{n:012d}")
    with serial_worker(fresh_url):
        with pytest.raises(RuntimeError):
            with work(fresh_url,"staging-test","test","rollback","same") as state:
                bulk_insert(state["connection"],"stock.building",[row(1)])
                raise RuntimeError("rollback staging creation")
        for n in (1,2):
            with work(fresh_url,"staging-test","test",str(n),"same") as state:
                c=state["connection"]
                bulk_insert(c,"stock.building",[row(n)])
                assert c.execute("SELECT count(*) FROM pg_class WHERE relnamespace=pg_my_temp_schema() AND relname LIKE 'stage_%'").fetchone()[0] == 1
    with psycopg.connect(fresh_url) as c:
        assert c.execute("SELECT count(*) FROM stock.building").fetchone()[0] == 2


@pytest.mark.postgis
def test_lineage_compatibility_retains_source_identifier_representation(fresh_url,tmp_path,monkeypatch):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from sweden_ubem.dataset import clean
    from sweden_ubem.hashing import sha256_file
    fixture=sources(tmp_path,monkeypatch)
    lineage = fixture.components() / "source-lineage.parquet"
    records = pq.read_table(lineage).to_pylist()
    for position, row in enumerate(records):
        row.update(revision_ts="2015-01-13 15:53:27.455000+00:00",
                   object_version=2 if position % 2 == 0 else None,
                   outline="TAKKANT", municipality="1485", house_number=2.0)
    pq.write_table(pa.Table.from_pylist(records), lineage)
    manifest_path = fixture.components() / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["source_lineage_sha256"] = sha256_file(lineage)
    manifest_path.write_text(json.dumps(manifest))
    initialize_sources(fresh_url,fixture,layers=["geometry"])
    with psycopg.connect(fresh_url) as c:
        rows=c.execute("SELECT source_fid,original_values FROM stock.component_source_feature").fetchall()
    assert rows
    assert all(original["source_fid"]==str(fid) for fid,original in rows)
    expected = {
        r["source_fid"]: {k: clean(v) for k, v in r.items()}
        for r in pq.read_table(fixture.components() / "source-lineage.parquet").to_pylist()
    }
    assert {fid: original for fid, original in rows} == expected
