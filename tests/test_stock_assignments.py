"""Stock selections preserve declared catalogue assumptions on independent records."""

from types import SimpleNamespace

import pandas as pd
import pytest
from shapely.geometry import box

from epsm_db import BuildingRecord, BuildingType
from sweden_ubem.stock import Stock
from sweden_ubem import stock as stock_module
from sweden_ubem.records import Receipt, RecordCollection


def make_cell(monkeypatch, *, era="SE.07", ventilation="FTX", climate_zone="SE.Zone3"):
    source = BuildingRecord("dataset", "one", BuildingType("multi_family"), box(0, 0, 10, 10), {})
    reader = SimpleNamespace(from_uuids=lambda ids: RecordCollection((source,), Receipt("dataset")))
    monkeypatch.setattr(stock_module.fetch_records, "using", lambda *args, **kwargs: reader)
    stock = Stock(SimpleNamespace(dataset_id="dataset", database_url="postgresql://unused"))
    cell = stock.cell(
        building_type="multi_family", era=era, ventilation=ventilation, climate_zone=climate_zone
    )
    cell._members = pd.DataFrame({"building_id": ["one"], "ventilation_system": ["FTX"]})
    return source, cell


def test_fully_specified_cell_attaches_independent_assumption_metadata(monkeypatch):
    source, cell = make_cell(monkeypatch)
    selected = next(iter(cell))
    assert selected is not source
    assert source.archetype_assignments == ()
    assignment = selected.archetype_assignments[0]
    assert assignment["kind"] == "assumption"
    assert assignment["source"] == "stock_cell"
    assert assignment["category"] == "multi_family"
    assert assignment["era"] == "SE.07"
    assert assignment["renovation_state"] is None
    assert assignment["construction_years"] == [2022, None]
    assert assignment["slot_id"] == cell.archetype_slot.slot_id
    assert len(assignment["catalogue_sha256"]) == 64
    restored = BuildingRecord.from_dict(selected.to_dict())
    assert restored.archetype_assignments == selected.archetype_assignments
    again = next(iter(cell))
    assignment["era"] = "edited"
    assert again.archetype_assignments[0]["era"] == "SE.07"


def test_partial_cell_does_not_invent_an_assignment(monkeypatch):
    source, cell = make_cell(monkeypatch, climate_zone=None)
    assert next(iter(cell)).archetype_assignments == ()
    assert source.archetype_assignments == ()


def test_se07_rejects_renovation_state(monkeypatch):
    _, cell = make_cell(monkeypatch)
    with pytest.raises(ValueError, match="no renovation states"):
        cell.stock.cell(building_type="multi_family", era="SE.07", renovation_state="existing")
