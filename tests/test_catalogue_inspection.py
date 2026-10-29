"""Stock classification uses only the public Data catalogue contract."""

from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from typing import ClassVar

import pytest
from shapely.geometry import box

from epsm_db import BuildingRecord, BuildingType, Stock
from epsm_db import stock as stock_module
from epsm_db._logging import operation


class InspectionCatalogue:
    """Narrow public inspection contract, with no construction or physics API."""

    dataset_id = "synthetic:catalogue"
    dimensions: ClassVar[dict] = {
        "categories": ("single_family", "multi_family", "office", "school"),
        "eras": {"SE.06": (2006, 2021), "SE.07": (2022, None)},
        "zones": ("SE.Zone1", "SE.Zone2", "SE.Zone3"),
        "states": ("existing", "standard", "advanced"),
    }
    archetype_records: ClassVar[dict] = {"mf_se07": {"category": "multi_family", "era": "SE.07"}}

    def __init__(self):
        self.years = []
        self.keys = []

    def era_for_year(self, year):
        self.years.append(year)
        return {2021: "SE.06", 2022: "SE.07"}[year]

    def ids(self, *, category=None):
        return ("mf_se07",)

    def get_slot(self, key):
        self.keys.append(key)
        return SimpleNamespace(key=key, slot_id=key.id, selection=None, archetype_id="mf_se07")

    def resolved_dict(self):
        return deepcopy({"dataset_id": self.dataset_id, "fixture": "inspection-only"})


class _Result(list):
    """Rows of a fake cursor: iterable, with fetchall and a count for fetchone."""

    def fetchall(self):
        return list(self)

    def fetchone(self):
        return {"n": len(self)}


class FakeConnection:
    def __init__(self, rows=()):
        self.rows, self.queries = list(rows), []

    def execute(self, query, values=None):
        self.queries.append((query.as_string(None) if hasattr(query, "as_string") else str(query), values))
        return _Result(self.rows)


def fake_db(connection=None):
    connection = connection or FakeConnection()

    @contextmanager
    def _read():
        yield connection, {"dataset_id": "synthetic"}

    return SimpleNamespace(_read=_read, _lake=SimpleNamespace(url="postgresql://unused"))


@pytest.fixture
def catalogue(monkeypatch):
    epsm_data = pytest.importorskip("epsm_data")
    catalogue = InspectionCatalogue()
    monkeypatch.setattr(epsm_data.Catalogue, "load", lambda **kwargs: catalogue)
    return catalogue


def test_stock_retains_public_catalogue_without_resolving_a_building(catalogue):
    stock = Stock(fake_db())
    assert stock.catalogue is catalogue
    cell = stock.cell("multi_family", "SE.07", ventilation="FTX", climate_zone="SE.Zone3")
    slot = cell.archetype_slot
    assert slot.key.category == "multi_family"
    assert slot.key.renovation_state is None
    assert len(catalogue.keys) == 1


def test_matrix_delegates_years_and_preserves_missing_and_generic_classifications(catalogue):
    rows = [
        {"building_type": "multi_family", "construction_year": 2021, "n": 2},
        {"building_type": "multi_family", "construction_year": 2022, "n": 3},
        {"building_type": "office", "construction_year": None, "n": 4},
        {"building_type": "industrial", "construction_year": 2022, "n": 5},
        {"building_type": "unknown", "construction_year": 0, "n": 6},
    ]
    matrix = Stock(fake_db(FakeConnection(rows))).matrix()
    assert list(matrix.columns) == ["SE.06", "SE.07", "Missing year", "Total"]
    assert matrix.loc["multi_family"].to_dict() == {"SE.06": 2, "SE.07": 3, "Missing year": 0, "Total": 5}
    assert matrix.loc["office", "Missing year"] == 4
    assert matrix.loc["generic"].to_dict() == {"SE.06": 0, "SE.07": 5, "Missing year": 6, "Total": 11}
    assert matrix.loc["TOTAL", "Total"] == 20
    assert catalogue.years == [2021, 2022, 2022]


def test_era_selection_uses_public_era_limits_without_model_defaults(catalogue):
    connection = FakeConnection()
    stock = Stock(fake_db(connection))
    len(stock.cell("multi_family", "SE.06"))
    query, values = connection.queries[-1]
    assert "f.construction_year<=%s" in query and values[-3:] == ["multi_family", 2006, 2021]
    len(stock.cell("multi_family", "SE.07"))
    query, values = connection.queries[-1]
    assert "f.construction_year<=%s" not in query and values[-2:] == ["multi_family", 2022]
    with pytest.raises(ValueError, match="Unknown catalogue era"):
        stock.cell("multi_family", "invalid")


def test_catalogue_assignment_metadata_is_detached_and_stays_an_assumption(catalogue, monkeypatch):
    original = BuildingRecord("synthetic", "one", BuildingType("multi_family"), box(0, 0, 8, 10), {})
    monkeypatch.setattr(stock_module.Stock, "iter_records", lambda self, *, batch_size=100: iter((original,)))
    cell = Stock(fake_db()).cell("multi_family", "SE.07", ventilation="FTX", climate_zone="SE.Zone3")
    selected = next(iter(cell))
    assignment = selected.archetype_assignments[0]
    assert assignment["catalogue_dataset"] == "synthetic:catalogue"
    assert assignment["kind"] == "assumption"
    assert assignment["source"] == "stock_cell"
    assert assignment["construction_years"] == [2022, None]
    assert assignment["catalogue_sha256"] == (
        "8457de94571e85cff155ca2669e12c6415888d2cc02033d03fd978006660ef2a"
    )
    assert original.archetype_assignments == ()
    assignment["era"] = "edited"
    assert next(iter(cell)).archetype_assignments[0]["era"] == "SE.07"


def test_catalogue_loads_forward_context_without_changing_classification(catalogue, monkeypatch):
    import epsm_data

    contexts = []

    def load(*, log_context=None):
        contexts.append(log_context)
        return catalogue

    monkeypatch.setattr(epsm_data.Catalogue, "load", load)

    @operation("database.catalogue.inspect")
    def inspect():
        Stock(fake_db()).matrix()
        Stock(fake_db()).cell("multi_family", "SE.07")

    inspect(log_context={"study_id": "catalogue-context"})
    assert len(contexts) == 2
    assert all(context and context["study_id"] == "catalogue-context" for context in contexts)
