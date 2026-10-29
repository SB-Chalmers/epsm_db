"""Stock cells preserve declared catalogue assumptions on independent records."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from shapely.geometry import box

from epsm_db import BuildingRecord, BuildingType, Stock
from epsm_db import stock as stock_module

pytest.importorskip("epsm_data")


class _Result(list):
    """Rows of a fake cursor: iterable, with fetchall and a count for fetchone."""

    def fetchall(self):
        return list(self)

    def fetchone(self):
        return {"n": len(self)}


class FakeConnection:
    """Answers the stock's SQL with prepared rows; records the statements it saw."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.queries = []

    def execute(self, query, values=None):
        self.queries.append((query.as_string(None) if hasattr(query, "as_string") else str(query), values))
        return _Result(self.rows)


def fake_db(connection):
    @contextmanager
    def _read():
        yield connection, {"dataset_id": "synthetic"}

    return SimpleNamespace(_read=_read, _lake=SimpleNamespace(url="postgresql://unused"))


def make_cell(monkeypatch, *, building_type="multi_family", era="SE.07", ventilation="FTX", climate_zone="SE.Zone3"):
    source = BuildingRecord("dataset", "one", BuildingType("multi_family"), box(0, 0, 10, 10), {})
    monkeypatch.setattr(stock_module.Stock, "iter_records", lambda self, *, batch_size=100: iter((source,)))
    stock = Stock(fake_db(FakeConnection()))
    cell = stock.cell(building_type, era, ventilation=ventilation, climate_zone=climate_zone)
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
    assert assignment["slot_id"] == cell.archetype_slot.slot_id == "multi_family:SE.07:SE.Zone3:default"
    assert "selection" not in assignment
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


def test_ventilation_filter_does_not_select_the_fabric_slot(monkeypatch):
    _, cell = make_cell(monkeypatch, ventilation=None)
    assert cell.archetype_slot.slot_id == "multi_family:SE.07:SE.Zone3:default"
    _, cell = make_cell(monkeypatch, era="SE.02", ventilation="S")
    assert cell.renovation_state == "existing"
    assert cell.archetype_slot.slot_id == "multi_family:SE.02:SE.Zone3:existing"
    assert cell.archetype.archetype_id == "mf_se02"


def test_se07_rejects_renovation_state(monkeypatch):
    _, cell = make_cell(monkeypatch)
    with pytest.raises(ValueError, match="no renovation states"):
        cell.stock.cell("multi_family", "SE.07", renovation_state="existing")
    with pytest.raises(ValueError, match="Unknown catalogue building type"):
        cell.stock.cell("industrial", "SE.07")


def test_unknown_type_cohort_uses_explicit_mf_assumption_without_changing_source(monkeypatch):
    source, cell = make_cell(monkeypatch, building_type="generic")
    selected = next(iter(cell))
    assert cell.archetype_slot.archetype_id == "mf_se07"
    assert cell.archetype.archetype_id == "mf_se07"
    assignment = selected.archetype_assignments[0]
    assert assignment["category"] == "generic"
    assert assignment["slot_id"] == "generic:SE.07:SE.Zone3:default"
    assert assignment["selection"] == {
        "requested_category": "generic",
        "donor_category": "multi_family",
        "donor_archetype_id": "mf_se07",
        "assumption_label": "Unknown building type — multifamily assumption",
    }
    assert selected.type == source.type
    assert source.archetype_assignments == ()
    restored = BuildingRecord.from_dict(selected.to_dict())
    assert restored.archetype_assignments == selected.archetype_assignments


def test_generic_selection_keeps_non_authored_types_in_sql(monkeypatch):
    connection = FakeConnection()
    stock = Stock(fake_db(connection))
    assert len(stock.cell("generic", "SE.02")) == 0
    query, values = connection.queries[-1]
    assert "NOT IN ('single_family','multi_family','school','office')" in query
    assert "generic" not in values
    assert values[-2:] == [1961, 1975]


def test_matrix_keeps_unknown_type_counts(monkeypatch):
    rows = [
        {"building_type": "unknown", "construction_year": 1970, "n": 4},
        {"building_type": "multi_family", "construction_year": 1970, "n": 3},
    ]
    stock = Stock(fake_db(FakeConnection(rows)))
    matrix = stock.matrix()
    assert matrix.loc["generic", "SE.02"] == 4
    assert matrix.loc["multi_family", "SE.02"] == 3
    assert matrix.loc["TOTAL", "Total"] == 7
    assert stock.matrix(building_type="generic").loc["generic", "Total"] == 4
