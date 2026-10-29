from copy import deepcopy
from datetime import date

import pytest
from shapely.geometry import box

from sweden_ubem.enums import BuildingType
from sweden_ubem.io import (
    AmbiguousRecordError,
    RecordNotFoundError,
    _epsg,
    _geometry,
    fetch_record,
    fetch_records,
)
from sweden_ubem.records import BuildingRecord, Receipt, RecordCollection


def make_record(
    *, storeys=1, basement=0, programmes=None, type=BuildingType.RESIDENTIAL_SINGLE_FAMILY
):
    cid = "efter_v260:10"
    geom = box(0, 0, 10, 10)
    obs = [
        {
            "certificate_id": cid,
            "source_field": k,
            "raw_values": [str(v)],
            "mapping_status": "verified",
        }
        for k, v in {
            "EgenAtemp": 90,
            "EgenAntalPlan": storeys,
            "EgenAntalKallarplan": basement,
            "EgenNybyggAr": 1977,
            **(programmes or {"EgenAtempBostad": 100}),
        }.items()
        if v is not None
    ]
    return BuildingRecord(
        "test",
        "00000000-0000-0000-0000-000000000001",
        type,
        geom,
        facts={
            "geometry_status": "resolved",
            "construction_year": 1977,
            "status_verified": True,
            "is_existing": True,
        },
        components=(
            {
                "component_id": "c1",
                "geometry_hash": "hash",
                "geometry": geom,
                "outline_labels": ["Fasad"],
            },
        ),
        heights=(
            {
                "component_id": "c1",
                "input_geometry_hash": "hash",
                "height_status": "determined",
                "roof_form": "flat",
                "representative_height_m": 3,
                "eave_height_m": 3,
                "details": {"evidence_resolution": "resolved"},
            },
        ),
        certificates=(
            {
                "certificate_id": cid,
                "certificate_date": date(2023, 1, 1),
                "date_status": "verified",
                "subject_count": 1,
                "loaded_building_count": 1,
                "matching_status": "fully_matched",
            },
        ),
        observations=tuple(obs),
        receipt=Receipt("test"),
    )


def test_no_match_and_ambiguity_have_receipts_and_candidates():
    record = make_record()
    receipt = Receipt("test")
    with pytest.raises(RecordNotFoundError) as exc:
        RecordCollection((), receipt).one()
    assert exc.value.receipt is receipt
    with pytest.raises(AmbiguousRecordError) as exc:
        RecordCollection((record, record), receipt).one()
    assert len(exc.value.candidates) == 2
    with pytest.raises(AmbiguousRecordError):
        RecordCollection((record,), Receipt("test", truncated=True)).one()


def test_municipality_names_resolve_independently_of_placeholder_db_labels():
    from sweden_ubem.io import _municipality_codes_by_name

    names = _municipality_codes_by_name()
    assert len(names) == 290
    assert len(set(names.values())) == 290
    for label in ("Uddevalla", " UDDEVALLA kommun ", "1485"):
        assert fetch_records._municipality(label) == ("f.municipality_code=%s", ["1485"])
    assert fetch_records._municipality("Göteborg")[1] == ["1480"]
    assert "mart.context_areas" in fetch_records._municipality("Unlisted municipality")[0]


def test_tied_unknown_dates_and_shared_epc_are_not_allocated():
    from dataclasses import replace

    original = make_record()
    cert = original.certificates[0]
    tied = replace(original, certificates=(cert, dict(cert, certificate_id="other")))
    assert tied.atemp_m2 is None and tied.selected_certificate is None
    unknown = replace(
        original, certificates=(cert, dict(cert, certificate_id="unknown", certificate_date=None))
    )
    assert unknown.selected_certificate is None
    shared = replace(original, certificates=(dict(cert, loaded_building_count=2),))
    assert shared.atemp_m2 is None and shared.programmes == {}
    assert original.atemp_m2 == 90


def test_conflicting_raw_values_and_partial_programmes_are_not_normalized():
    from dataclasses import replace

    r = make_record()
    observations = deepcopy(r.observations)
    observations[0]["raw_values"] = ["90", "100"]
    r = replace(r, observations=observations)
    assert r.observation("EgenAtemp") == (None, "conflicting")
    with pytest.raises(ValueError, match="sum"):
        _ = make_record(programmes={"EgenAtempBostad": 80}).programmes
    assert make_record(programmes={"EgenAtempBostad": 80, "EgenAtempKontor": 20}).programmes == {
        "residential": 0.8,
        "office": 0.2,
    }


def test_geometry_and_coordinate_validation_precede_database_access():
    assert _epsg("EPSG:3006") == 3006
    assert _geometry("POLYGON ((0 0,1 0,1 1,0 1,0 0))").area == 1
    with pytest.raises(ValueError):
        _geometry("POLYGON EMPTY")
    with pytest.raises(ValueError):
        fetch_records.within_boundary("POINT (1 2)")
    with pytest.raises(ValueError):
        fetch_record.from_point(float("nan"), 0)
    with pytest.raises(ValueError):
        fetch_records.from_footprint(box(0, 0, 1, 1), min_overlap_fraction=1.1)


def test_connection_binding_is_local_not_global():
    bound = fetch_records.using("private-url", dataset="sweden")
    assert bound.dataset == "sweden" and fetch_records.database_url is None
    assert "private-url" not in str(make_record().receipt)
