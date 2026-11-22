"""Re-reading building purposes into an existing snapshot, on a disposable database."""

import os
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

from epsm_db import Lake

pytestmark = pytest.mark.postgis

XML = """<?xml version="1.0" encoding="UTF-8"?>
<Leverans xmlns="urn:lm">
  <Byggnad>
    <objektidentitet>school-1</objektidentitet>
    <objektstatus>Gällande</objektstatus>
    <Byggnadsattribut><objektversion>1</objektversion><nybyggnadsar>1968</nybyggnadsar></Byggnadsattribut>
    <Samhallsfunktion><huvudandamal>true</huvudandamal><andamal>Skola</andamal></Samhallsfunktion>
    <Verksamhet><huvudandamal>false</huvudandamal><andamal>Kontor</andamal></Verksamhet>
  </Byggnad>
  <Byggnad>
    <objektidentitet>home-1</objektidentitet>
    <objektstatus>Gällande</objektstatus>
    <Byggnadsattribut><objektversion>1</objektversion><nybyggnadsar>1975</nybyggnadsar></Byggnadsattribut>
    <Bostad><huvudandamal>true</huvudandamal><andamal>Småhus friliggande</andamal></Bostad>
  </Byggnad>
  <Byggnad>
    <objektidentitet>edited-1</objektidentitet>
    <objektstatus>Gällande</objektstatus>
    <Byggnadsattribut><objektversion>1</objektversion><nybyggnadsar>1990</nybyggnadsar></Byggnadsattribut>
    <Samhallsfunktion><huvudandamal>true</huvudandamal><andamal>Vårdcentral</andamal></Samhallsfunktion>
  </Byggnad>
</Leverans>
"""
MANIFEST = """schema_version = 1
root = "."
srid = 3006

[[sources]]
name = "lm-building-register"
version = "test-1"
kind = "buildings"
path = "buildings/*.xml"
"""
# What the parser stored before it read the register's spelling of the element.
FORGET = """UPDATE lake.building SET
 purposes=COALESCE((SELECT jsonb_agg(x) FROM jsonb_array_elements(purposes) x
                    WHERE x->>'category'<>'Samhällsfunktion'),'[]'),
 attributes=jsonb_set(attributes,'{purposes}',
   COALESCE((SELECT jsonb_agg(x) FROM jsonb_array_elements(attributes->'purposes') x
             WHERE x->>'category'<>'Samhällsfunktion'),'[]'))"""


@pytest.fixture()
def snapshot(tmp_path):
    """A fresh lake in its own database, with three buildings imported as the old parser left them."""
    url = os.environ.get("EPSM_LAKE_TEST_URL")
    if not url:
        pytest.skip("Set EPSM_LAKE_TEST_URL to a disposable database copy")
    name = f"epsm_api_validation_reparse_{os.getpid()}"
    own = make_conninfo(**{**conninfo_to_dict(url), "dbname": name})
    try:
        with psycopg.connect(url, autocommit=True) as c:
            c.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
            c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    except psycopg.errors.InsufficientPrivilege:
        pytest.skip("The validation role cannot create a disposable database")
    try:
        with psycopg.connect(own, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS postgis")
        lake = Lake(own)
        assert lake.initialize()["created"]
        (tmp_path / "buildings").mkdir()
        (tmp_path / "buildings/part.xml").write_text(XML, encoding="utf-8")
        manifest = tmp_path / "lake.toml"
        manifest.write_text(MANIFEST)
        assert lake.ingest(manifest)["completed_files"] == 1
        with lake.connect(autocommit=True) as c:
            c.execute(FORGET)
            # A building whose stored record no longer equals the file must be left alone.
            c.execute("""UPDATE lake.building SET attributes=jsonb_set(attributes,'{object_status}','"Avregistrerad"')
                         WHERE external_id='edited-1'""")
        lake.link()
        lake.publish()
        yield lake, manifest
    finally:
        with psycopg.connect(url, autocommit=True) as c:
            c.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def stored(lake):
    with lake.connect(autocommit=True) as c:
        rows = c.execute("""SELECT external_id,
            (SELECT array_agg(x->>'category' ORDER BY x->>'category') FROM jsonb_array_elements(purposes) x),
            (SELECT array_agg(x->>'category' ORDER BY x->>'category') FROM jsonb_array_elements(attributes->'purposes') x)
            FROM lake.building ORDER BY 1""").fetchall()
        revision = c.execute("SELECT revision,published_revision FROM lake.publication").fetchone()
    return {r[0]: (r[1], r[2]) for r in rows}, revision


def test_reparse_restores_only_the_parsed_purposes(snapshot):
    lake, manifest = snapshot
    before, (revision, published) = stored(lake)
    assert before["school-1"] == (["Verksamhet"], ["Verksamhet"])
    assert revision == published

    dry = lake.reparse_purposes(manifest, dry_run=True)
    assert (dry["records_read"], dry["buildings_matched"], dry["buildings_changed"]) == (3, 2, 1)
    assert dry["next"] == "nothing written"
    assert stored(lake) == (before, (revision, published))

    done = lake.reparse_purposes(manifest)
    assert (done["files"], done["buildings_matched"], done["buildings_changed"]) == (1, 2, 1)
    assert done["next"] == "epsm-db link; epsm-db publish"
    after, (new_revision, still_published) = stored(lake)
    assert after["school-1"] == (["Samhällsfunktion", "Verksamhet"], ["Samhällsfunktion", "Verksamhet"])
    assert after["home-1"] == before["home-1"] == (["Bostad"], ["Bostad"])
    assert after["edited-1"] == before["edited-1"] == (None, None)
    assert new_revision == revision + 1 and still_published == published

    again = lake.reparse_purposes(manifest)
    assert again["buildings_changed"] == 0 and again["next"] == "nothing written"
    assert stored(lake)[1] == (new_revision, published)
    lake.link()
    lake.publish()
    with lake.connect(autocommit=True) as c:
        assert c.execute("SELECT published_revision=revision FROM lake.publication").fetchone()[0]


def test_reparse_refuses_changed_or_unknown_sources(snapshot):
    lake, manifest = snapshot
    source = Path(manifest).parent / "buildings/part.xml"
    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    with pytest.raises(ValueError, match="modification time"):
        lake.reparse_purposes(manifest)
    assert lake.reparse_purposes(manifest, allow_mtime_change=True, dry_run=True)["buildings_changed"] == 1
    source.write_text(XML + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed after import"):
        lake.reparse_purposes(manifest, allow_mtime_change=True)
    other = Path(manifest).with_name("other.toml")
    other.write_text(MANIFEST.replace("test-1", "test-2"))
    with pytest.raises(ValueError, match="not imported with this version"):
        lake.reparse_purposes(other)
