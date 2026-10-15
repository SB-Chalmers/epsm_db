"""Source and wheel installations locate the same owned SQL and inventory files."""

from pathlib import Path

import pytest


def test_source_resource_location_keeps_existing_layout():
    from sweden_ubem.resources import resource_path

    root = Path(__file__).resolve().parents[1]
    assert resource_path("sql/baselines/0.0.4").is_dir()
    assert resource_path("config/source_inventory.toml").read_bytes() == (
        root / "config/source_inventory.toml").read_bytes()


def test_installed_resource_location_uses_its_installation_prefix(tmp_path, monkeypatch):
    from sweden_ubem import resources

    monkeypatch.setattr(resources, "__file__", str(tmp_path / "lib/python/site-packages/sweden_ubem/resources.py"))
    monkeypatch.setattr(resources.sysconfig, "get_path", lambda _: str(tmp_path))
    assert resources.resource_path("sql/baselines/0.0.4") == tmp_path / "share/epsm_db/sql/baselines/0.0.4"


def test_missing_baseline_fails_before_database_access(tmp_path, monkeypatch):
    from sweden_ubem import database

    def forbidden(_url):
        raise AssertionError("Missing SQL must fail before opening a database")

    monkeypatch.setattr(database, "_connect", forbidden)
    with pytest.raises(FileNotFoundError, match="baseline"):
        database.initialize_database("unused", migrations=tmp_path / "missing")
