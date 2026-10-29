"""Source and wheel installations locate the same owned SQL and lookup files."""

from importlib.resources import files
from pathlib import Path

import pytest

from epsm_db.lake import database


def test_source_resource_location_keeps_existing_layout():
    root = Path(__file__).resolve().parents[1]
    assert database.resource("001_schema.sql") == root / "sql/lake/001_schema.sql"
    assert files("epsm_db").joinpath("municipality_names.json").read_text().startswith("{")


def test_installed_resource_location_uses_its_installation_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "__file__", str(tmp_path / "lib/python/site-packages/epsm_db/lake/database.py"))
    monkeypatch.setattr(database.sysconfig, "get_path", lambda _: str(tmp_path))
    packaged = tmp_path / "share/epsm_db/sql/lake/001_schema.sql"
    packaged.parent.mkdir(parents=True)
    packaged.write_text("-- packaged")
    assert database.resource("001_schema.sql") == packaged
    with pytest.raises(FileNotFoundError, match="Missing lake SQL resource"):
        database.resource("missing.sql")
