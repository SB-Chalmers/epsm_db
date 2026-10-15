"""Stock conversion preserves every outcome and uses the canonical physical model."""

from copy import deepcopy
from dataclasses import replace

import pytest
from shapely.geometry import Polygon, box
from test_stock_assignments import make_cell

epsm = pytest.importorskip("epsm")


def test_cell_and_record_conversion_agree_without_weather(monkeypatch):
    source, cell = make_cell(monkeypatch)
    before = deepcopy(source.to_dict())
    models, receipts = cell.to_epsm(storeys=2, storey_height_m=3)
    assert len(models) == len(receipts) == 1
    assert isinstance(models[0], epsm.Building)
    direct = epsm.Building.from_record(next(iter(cell)), storeys=2, storey_height_m=3)
    assert models[0].geometry == direct.geometry
    assert models[0].construction_set is None and models[0].hvac is None
    assert models[0].to_dict()["source_record"]["archetype_assignments"]
    assert not receipts[0].ready_for_simulation
    assert receipts[0].fields["model_readiness"]["requirements"]
    assert not receipts[0].errors
    assert source.to_dict() == before


def test_mixed_batch_keeps_incomplete_drafts_and_invalid_receipts(monkeypatch):
    source, cell = make_cell(monkeypatch)
    valid = replace(source, building_id="valid", geometry=box(0, 0, 10, 10))
    incomplete = replace(source, building_id="missing", geometry=None)
    invalid = replace(
        source, building_id="invalid", geometry=Polygon([(0, 0), (10, 10), (0, 10), (10, 0)])
    )
    monkeypatch.setattr(type(cell), "iter_records", lambda self: iter((valid, incomplete, invalid)))
    models, receipts = cell.to_epsm(storeys=2, storey_height_m=3)
    assert [model.identifier for model in models] == ["valid", "missing"]
    assert [receipt.building_id for receipt in receipts] == ["valid", "missing", "invalid"]
    assert models[1].geometry is None
    assert receipts[2].errors and not receipts[2].ready_for_simulation
    assert not source.receipt.errors
    assert models[0].to_dict()["source_record"]["building_id"] == "valid"


@pytest.mark.parametrize(
    "legacy",
    [
        {"weather": "weather.epw"},
        {"policy": "ubem_v1"},
        {"overrides": {}},
        {"preflight": False},
        {"energyplus": "/engine"},
    ],
)
def test_legacy_arguments_have_an_actionable_migration_error(monkeypatch, legacy):
    _, cell = make_cell(monkeypatch)
    with pytest.raises(TypeError, match="to_epsm_legacy"):
        cell.to_epsm(**legacy)


def test_explicit_legacy_route_preserves_existing_options(monkeypatch):
    _, cell = make_cell(monkeypatch)
    from epsm.legacy_records import EPSMBuilding

    captured = {}

    def convert(records, **kwargs):
        captured.update(kwargs)
        assert len(tuple(records)) == 1
        return ("models", "receipts")

    monkeypatch.setattr(EPSMBuilding, "from_records", convert)
    assert cell.to_epsm_legacy(weather="declared.epw") == ("models", "receipts")
    assert captured["weather"] == "declared.epw"
    assert captured["overrides"]["climate_zone"] == "SE.Zone3"
