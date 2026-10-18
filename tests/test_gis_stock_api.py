"""Pure API, portable evidence and modern EPSM conversion acceptance."""

import json
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace

import pytest
from shapely.geometry import Polygon, box

from epsm_db import BuildingRecord, BuildingType, Database, Receipt, Stock


def record(identifier="one", geometry=None):
    return BuildingRecord(
        "snapshot",
        identifier,
        BuildingType.RESIDENTIAL_MULTIFAMILY,
        geometry,
        {"construction_year": 1960},
        receipt=Receipt("snapshot", identifier),
        areas=({"area_type": "deso", "boundary_version": "2025", "area_code": "x"},),
        heights=({"component_id": 17, "representative_height_m": 9},),
        _database_url="postgresql://user:secret@host/database",
    )


def test_geographic_selection_is_lazy_without_optional_packages():
    code = """
import builtins
original = builtins.__import__
def guarded(name,*args,**kwargs):
    if name.split('.')[0] in {'epsm','epsm_core','epsm_data'}:
        raise AssertionError('optional model import: '+name)
    return original(name,*args,**kwargs)
builtins.__import__ = guarded
from epsm_db import Database, Stock, BuildingRecord
stock = Database('postgresql://invalid/no_connection').stock(municipality='Uddevalla')
assert stock.describe()['municipality']=='1485'
assert 'lazy' in repr(stock)
assert 'invalid' not in repr(stock.db)
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_selectors_validate_early():
    db = Database("postgresql://invalid")
    for bbox in [(0, 0, 0, 1), (0, 0, float("inf"), 1)]:
        with pytest.raises(ValueError):
            db.stock(bbox=bbox)
    with pytest.raises(ValueError):
        db.stock(area_type="deso")
    with pytest.raises(ValueError):
        db.stock(municipality="not a municipality")
    assert db.stock(municipality=485).describe()["municipality"] == "0485"


def test_portable_additions_and_old_envelopes_roundtrip():
    source = record(geometry=box(0, 0, 10, 10))
    payload = json.loads(json.dumps(source.to_dict()))
    assert "secret" not in json.dumps(payload)
    restored = BuildingRecord.from_dict(payload)
    assert restored.areas == source.areas and restored.heights == source.heights
    assert restored._database_url is None
    payload["record"].pop("areas")
    payload["record"].pop("property_parts")
    assert BuildingRecord.from_dict(payload).areas == ()


def test_conversion_preserves_every_outcome_and_source(monkeypatch):
    pytest.importorskip("epsm")
    source = record("valid", box(0, 0, 10, 10))
    missing = replace(source, building_id="missing", geometry=None)
    invalid = replace(
        source, building_id="invalid", geometry=Polygon([(0, 0), (10, 10), (0, 10), (10, 0)])
    )
    before = deepcopy(source.to_dict())
    stock = Stock(Database("postgresql://unused"))
    monkeypatch.setattr(
        Stock, "iter_records", lambda self, **kw: (r for r in (source, missing, invalid))
    )
    models, receipts = stock.to_epsm(storeys=2, storey_height_m=3)
    assert [m.identifier for m in models] == ["valid", "missing"]
    assert [r.building_id for r in receipts] == ["valid", "missing", "invalid"]
    assert models[0].construction_set is None
    assert models[1].geometry is None
    assert receipts[-1].errors
    assert all(not r.ready_for_simulation for r in receipts)
    assert source.to_dict() == before
    assert models[0].to_dict()["source_record"]["portable_record"]["record"]["heights"]


def test_custom_archetype_and_offline_model_roundtrip(monkeypatch):
    epsm = pytest.importorskip("epsm")
    from epsm_data import Catalogue, Archetype

    source = record("custom", box(0, 0, 10, 10))
    stock = Stock(Database("postgresql://unused"))
    monkeypatch.setattr(Stock, "iter_records", lambda self, **kw: (r for r in [source]))
    selected = Catalogue.load().get("mf_se02", state="existing")
    models, receipts = stock.to_epsm(
        archetype=selected, calendar={"calendar_year": 2023}, storeys=2, storey_height_m=3
    )
    model = models[0]
    assert model.construction_set is not None
    assert model.hvac is None
    assert model.archetype_applications
    restored = epsm.Building.from_dict(json.loads(json.dumps(model.to_dict())))
    assert restored.archetype_applications
    evidence = restored.to_dict()["source_record"]["portable_record"]
    assert evidence["record"]["building_id"] == "custom"
    assert receipts[0].identities["archetype_id"] == selected.archetype_id
    authored = Archetype("project:partial", "Partial definition")
    models, receipts = stock.to_epsm(archetype=authored, storeys=2, storey_height_m=3)
    assert models and models[0].archetype_applications[-1].requirements


def test_cell_archetype_needs_no_climate_or_ventilation(monkeypatch):
    pytest.importorskip("epsm_data")
    stock = Stock(Database("postgresql://unused"))
    cell = stock.cell(building_type="multi_family", era="SE.02")
    assert cell.archetype == stock.catalogue.get("mf_se02", state="existing")
    assert cell._archetype("cell") is cell.archetype
    assert cell.climate_zone is None and cell.ventilation is None
    with pytest.raises(ValueError):
        stock._archetype("cell")
    monkeypatch.setattr(stock.catalogue, "ids", lambda **kwargs: ())
    with pytest.raises(ValueError):
        stock.cell(building_type="generic", era="SE.02").archetype


def test_generator_logging_context_and_no_handlers():
    import logging
    from epsm_db._logging import current_context, operation

    before = logging.getLogger("epsm_db").handlers[:]

    @operation("test.read")
    def generated():
        assert current_context()["operation_id"]
        yield 1

    generator = generated(log_context={"study_id": "one"})
    assert next(generator) == 1
    assert current_context() == {}
    generator.close()
    assert logging.getLogger("epsm_db").handlers == before
