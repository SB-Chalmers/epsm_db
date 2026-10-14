import json
from datetime import date

from shapely.geometry import box

from epsm_db import BuildingRecord, BuildingType, Stock
from sweden_ubem.stock import Stock as ExistingStock


def test_record_roundtrip_has_no_credentials_and_keeps_observations():
    record = BuildingRecord(
        "dataset",
        "one",
        BuildingType("single_family"),
        box(0, 0, 10, 10),
        {"construction_year": 2022},
        observations=(
            {"source_field": "area", "raw_values": ["100"], "unit": "m2", "date": date(2026, 1, 1)},
        ),
        _database_url="postgresql://secret",
    )
    payload = json.dumps(record.to_dict())
    assert "secret" not in payload and "_database_url" not in payload
    restored = BuildingRecord.from_dict(json.loads(payload))
    assert restored.observations == record.observations
    assert restored.geometry.equals(record.geometry)
    assert restored._database_url is None
    assert Stock is ExistingStock


def test_roundtrip_preserves_nested_component_and_address_geometries():
    from shapely.geometry import Point

    footprint = box(0, 0, 10, 10)
    record = BuildingRecord(
        "dataset",
        "one",
        BuildingType("single_family"),
        footprint,
        {},
        components=({"component_id": "one:shape", "geometry": footprint},),
        addresses=({"address_id": "address", "geometry": Point(2, 3)},),
    )
    restored = BuildingRecord.from_dict(json.loads(json.dumps(record.to_dict())))
    assert restored.components[0]["geometry"].equals(footprint)
    assert restored.addresses[0]["geometry"].equals(Point(2, 3))
    assert restored.components[0]["component_id"] == "one:shape"
    assert restored._database_url is None


def test_record_and_receipt_inspection_is_bounded_and_connection_free(monkeypatch):
    from epsm_db import Receipt

    def forbidden(*args, **kwargs):
        raise AssertionError("Inspection must not query the database")

    monkeypatch.setattr("sweden_ubem.database._connect", forbidden)
    unusual = "long\\\n\t\x00🧱" * 200
    receipt = Receipt(
        unusual,
        building_id=unusual,
        fields={"height": {"value": None}},
        errors=["an unresolved source"],
    )
    record = BuildingRecord(
        unusual,
        unusual,
        BuildingType("single_family"),
        None,
        {"year": 2022},
        receipt=receipt,
        _database_url="private-secret",
    )
    for obj in (record, receipt):
        assert len(repr(obj)) <= 160
        assert len(str(obj)) <= 160
        assert "\n" not in repr(obj) and "\n" not in str(obj)
        assert "private-secret" not in repr(obj)
        assert obj.summary()["dataset_id"] == unusual
    assert record.summary()["facts"] == {"year": 2022}
    record.summary()["facts"]["year"] = 1900
    assert record.facts["year"] == 2022
    assert "an unresolved source" in receipt.report()
