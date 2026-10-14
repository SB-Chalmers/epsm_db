from datetime import date
import json

from shapely.geometry import box

from epsm_db import BuildingRecord, BuildingType, Stock
from sweden_ubem.stock import Stock as ExistingStock


def test_record_roundtrip_has_no_credentials_and_keeps_observations():
    record = BuildingRecord("dataset", "one", BuildingType("single_family"), box(0, 0, 10, 10),
                            {"construction_year": 2022}, observations=({"source_field": "area",
                                "raw_values": ["100"], "unit": "m2", "date": date(2026, 1, 1)},),
                            _database_url="postgresql://secret")
    payload = json.dumps(record.to_dict())
    assert "secret" not in payload and "_database_url" not in payload
    restored = BuildingRecord.from_dict(json.loads(payload))
    assert restored.observations == record.observations
    assert restored.geometry.equals(record.geometry)
    assert restored._database_url is None
    assert Stock is ExistingStock
