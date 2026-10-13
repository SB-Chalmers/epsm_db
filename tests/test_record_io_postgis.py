"""Exercise spatial semantics in a unique disposable database only."""

import pytest
from fixture_factory import BUILDINGS, synthetic_sources
from pyproj import Transformer
from shapely.geometry import box
from test_postgis_integration import test_url as _test_url

from sweden_ubem.database import _connect
from sweden_ubem.dataset import load_dataset
from sweden_ubem.ingest import initialize_sources
from sweden_ubem.io import (
    AmbiguousRecordError,
    BoundaryUnavailableError,
    RecordNotFoundError,
    fetch_record,
    fetch_records,
)

test_url = _test_url


@pytest.fixture(scope="module")
def reader(test_url, tmp_path_factory):
    sources = synthetic_sources(tmp_path_factory.mktemp("record-api-sources"))
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("UBEM_SCB_SNAPSHOT", str(sources.path("scb_snapshot_selected")))
        initialize_sources(test_url, sources)
        load_dataset(test_url)
    return fetch_records.using(test_url, dataset="uddevalla")


@pytest.mark.postgis
def test_uuid_point_reprojection_area_and_footprint_match(reader):
    record = reader.from_uuid(BUILDINGS[0]).one()
    point = record.geometry.representative_point()
    found = reader.from_point(point.x, point.y).one()
    assert found.building_id == record.building_id
    lon, lat = Transformer.from_crs(3006, 4326, always_xy=True).transform(point.x, point.y)
    assert reader.from_point(lon, lat, epsg=4326).one().building_id == record.building_id
    assert reader.from_footprint(record.geometry).one().building_id == record.building_id
    with pytest.raises(RecordNotFoundError):
        reader.from_point(point.x, point.y, min_footprint_area_m2=record.geometry.area + 1).one()
    assert record.components and record.heights and record.receipt.snapshot


@pytest.mark.postgis
def test_pagination_has_no_duplicates_and_named_scopes_are_explicit(reader):
    first = reader.from_municipality("1485", limit=1)
    named = reader.from_municipality("Uddevalla kommun", limit=1)
    assert named[0].building_id == first[0].building_id
    assert first.receipt.truncated and first.receipt.next_cursor
    second = reader.from_municipality("1485", limit=1, after_uuid=first.receipt.next_cursor)
    assert not second.receipt.truncated
    assert first[0].building_id != second[0].building_id
    records = reader.within_boundary(box(-1, -1, 120, 30))
    assert len(records) == 2
    clipped = reader.within_boundary(box(0, 0, 1, 1))
    contained = reader.within_boundary(box(0, 0, 1, 1), spatial_match="covered_by")
    assert len(clipped) == 1 and len(contained) == 0
    with pytest.raises(BoundaryUnavailableError, match="not a zero-building"):
        reader.from_deso("not-imported")


@pytest.mark.postgis
def test_nearest_sorts_distance_and_support_is_not_implicitly_included(reader):
    nearest = reader.nearest(100, 2, max_distance_m=200)
    distances = [r.facts["query_distance_m"] for r in nearest]
    assert distances == sorted(distances)
    assert len(reader.from_bbox(-1, -1, 120, 30)) == 2
    assert (
        len(reader.from_bbox(-1, -1, 120, 30, include_support=True, include_other_statuses=True))
        == 3
    )


@pytest.mark.postgis
def test_shared_address_returns_candidates_without_choosing_largest(reader):
    url = reader.database_url
    with _connect(url) as c:
        c.execute("""UPDATE stock.address_place SET street_name='Testgatan',address_number='1',locality='Testort'
                     WHERE address_place_id='a1'""")
        c.execute(
            """INSERT INTO stock.entrance(dataset_id,entrance_id,building_id,address_place_id,original_values,run_id)
           SELECT dataset_id,'record-api-shared-address',%s::uuid,address_place_id,'{}'::jsonb,run_id
           FROM stock.entrance WHERE building_id=%s::uuid LIMIT 1""",
            (BUILDINGS[1], BUILDINGS[0]),
        )
    try:
        address = reader.from_uuid(BUILDINGS[0]).one().addresses[0]
        single = fetch_record.using(url, dataset="uddevalla")
        with pytest.raises(AmbiguousRecordError) as exc:
            single.from_address(street=address["street_name"], number=address["address_number"])
        assert len(exc.value.candidates) == 2
        assert (
            len(
                reader.from_address(street=address["street_name"], number=address["address_number"])
            )
            == 2
        )
    finally:
        with _connect(url) as c:
            c.execute("DELETE FROM stock.entrance WHERE entrance_id='record-api-shared-address'")
