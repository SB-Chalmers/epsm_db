"""Legacy SQL stock classification retains the public Data catalogue contract."""

from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from typing import ClassVar

import pandas as pd
import pytest
from shapely.geometry import box

from epsm_db import BuildingRecord, BuildingType
from sweden_ubem.stock import Stock
from sweden_ubem import explore
from sweden_ubem import stock as stock_module
from sweden_ubem.records import Receipt, RecordCollection


class InspectionCatalogue:
    """Narrow public inspection contract, with no construction or physics API."""

    dataset_id = "synthetic:catalogue"
    dimensions: ClassVar[dict] = {
        "categories": ("single_family", "multi_family", "office", "school", "generic"),
        "eras": {"SE.06": (2006, 2021), "SE.07": (2022, None)},
        "ventilation": ("S", "F", "FT", "FTX"),
        "zones": ("SE.Zone1", "SE.Zone2", "SE.Zone3"),
        "states": ("existing", "standard", "advanced"),
    }

    def __init__(self):
        self.years = []
        self.keys = []

    def era_for_year(self, year):
        self.years.append(year)
        return {2021: "SE.06", 2022: "SE.07"}[year]

    def get_slot(self, key):
        self.keys.append(key)
        return SimpleNamespace(key=key, slot_id=key.id)

    def effective_configuration(self):
        return deepcopy({"dataset_id": self.dataset_id, "fixture": "inspection-only"})


@pytest.fixture
def catalogue(monkeypatch):
    import epsm_data

    catalogue = InspectionCatalogue()
    monkeypatch.setattr(epsm_data.Catalogue, "load", lambda: catalogue)
    return catalogue


def test_stock_retains_public_catalogue_without_resolving_a_building(catalogue):
    stock = Stock(SimpleNamespace(dataset_id="synthetic", database_url="unused"))
    assert stock.catalogue is catalogue
    assert "synthetic:catalogue" in repr(stock)
    cell = stock.cell(building_type="multi_family", era="SE.07",
                      ventilation="FTX", climate_zone="SE.Zone3")
    slot = cell.archetype_slot
    assert slot.key.category == "multi_family"
    assert slot.key.renovation_state is None
    assert len(catalogue.keys) == 1


def test_exploration_delegates_years_and_preserves_missing_and_generic_classifications(
        catalogue, monkeypatch):
    rows = [
        {"building_type": "multi_family", "construction_year": 2021, "buildings": 2},
        {"building_type": "multi_family", "construction_year": 2022, "buildings": 3},
        {"building_type": "office", "construction_year": None, "buildings": 4},
        {"building_type": "industrial", "construction_year": 2022, "buildings": 5},
        {"building_type": "unknown", "construction_year": 0, "buildings": 6},
    ]
    monkeypatch.setattr(explore, "_rows", lambda *args, **kwargs: deepcopy(rows))
    database = object.__new__(explore.UBEM)
    database.database_url, database.dataset_id = "unused", "synthetic"
    matrix = database.archetype_matrix()
    assert list(matrix.columns) == ["SE.06", "SE.07", "Missing year", "Total"]
    assert matrix.loc["multi_family"].to_dict() == {
        "SE.06": 2, "SE.07": 3, "Missing year": 0, "Total": 5,
    }
    assert matrix.loc["office", "Missing year"] == 4
    assert matrix.loc["generic"].to_dict() == {
        "SE.06": 0, "SE.07": 5, "Missing year": 6, "Total": 11,
    }
    assert matrix.loc["TOTAL", "Total"] == 20
    assert catalogue.years == [2021, 2022, 2022]


def test_ventilation_selection_uses_public_era_limits_without_model_defaults(catalogue, monkeypatch):
    queries = []

    def execute(query, params):
        queries.append((query, params))
        return []

    @contextmanager
    def connection(url):
        yield SimpleNamespace(execute=execute)

    monkeypatch.setattr(explore, "_connect", connection)
    database = object.__new__(explore.UBEM)
    database.database_url, database.dataset_id = "unused", "synthetic"
    database.ventilation_buildings(building_type="multi_family", era="SE.06")
    assert "f.construction_year <= %s" in queries[0][0]
    assert tuple(queries[0][1]) == ("synthetic", 2006, 2021, "multi_family")
    database.ventilation_buildings(building_type="multi_family", era="SE.07")
    assert "f.construction_year <= %s" not in queries[1][0]
    assert tuple(queries[1][1]) == ("synthetic", 2022, "multi_family")
    with pytest.raises(ValueError, match="Unknown EPSM era"):
        database.ventilation_buildings(building_type="multi_family", era="invalid")


def test_catalogue_assignment_metadata_is_detached_and_stays_an_assumption(catalogue, monkeypatch):
    original = BuildingRecord("synthetic", "one", BuildingType("multi_family"), box(0, 0, 8, 10), {})
    reader = SimpleNamespace(from_uuids=lambda ids: RecordCollection((original,), Receipt("synthetic")))
    monkeypatch.setattr(stock_module.fetch_records, "using", lambda *args, **kwargs: reader)
    stock = Stock(SimpleNamespace(dataset_id="synthetic", database_url="unused"))
    cell = stock.cell(building_type="multi_family", era="SE.07",
                      ventilation="FTX", climate_zone="SE.Zone3")
    cell._members = pd.DataFrame({"building_id": ["one"]})
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

    from sweden_ubem._logging import operation

    contexts = []

    def load(*, log_context=None):
        contexts.append(log_context)
        return catalogue

    @contextmanager
    def connection(url):
        yield SimpleNamespace(execute=lambda *args: [])

    monkeypatch.setattr(epsm_data.Catalogue, "load", load)
    monkeypatch.setattr(explore, "_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(explore, "_connect", connection)
    database = object.__new__(explore.UBEM)
    database.database_url, database.dataset_id = "unused", "synthetic"

    @operation("database.catalogue.inspect")
    def inspect():
        Stock(database)
        database.archetype_matrix()
        database.ventilation_buildings(building_type="multi_family", era="SE.07")

    inspect(log_context={"study_id": "catalogue-context"})
    assert len(contexts) == 3
    assert all(context and context["study_id"] == "catalogue-context" for context in contexts)
    assert len({context["operation_id"] for context in contexts}) == 1
