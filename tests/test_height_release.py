import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sweden_ubem.config import (
    HeightReleaseBlocked,
    REQUIRED_HEIGHT_COLUMNS,
    validate_height_release,
)
from sweden_ubem.hashing import sha256_file


def test_publication_blockers_are_reported_verbatim(tmp_path):
    inspected = json.loads(
        (Path(__file__).parent / "fixtures/tower-publication-blocked.json").read_text()
    )
    fields = [(name, pa.string()) for name in inspected.pop("component_catalogue_columns")]
    pq.write_table(
        pa.Table.from_arrays(
            [pa.array([], type=kind) for _, kind in fields], names=[name for name, _ in fields]
        ),
        tmp_path / "component-height-catalogue.parquet",
    )
    (tmp_path / "manifest.json").write_text(json.dumps(inspected))
    with pytest.raises(HeightReleaseBlocked) as caught:
        validate_height_release(tmp_path)
    assert caught.value.report["missing_height_records"] == 76998
    assert caught.value.report["unresolved_representation_components"] == 14779
    assert any("EPSM preflight" in item for item in caught.value.report["blockers"])


def test_published_height_contract_allows_additional_columns(tmp_path):
    fields = [(name, pa.string()) for name in sorted(REQUIRED_HEIGHT_COLUMNS)] + [
        ("extra", pa.int64())
    ]
    pq.write_table(
        pa.Table.from_arrays(
            [pa.array([], type=kind) for _, kind in fields], names=[name for name, _ in fields]
        ),
        tmp_path / "component-height-catalogue.parquet",
    )
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "release_status": "published",
                "missing_height_records": 0,
                "unresolved_representation_components": 0,
                "epsm_preflight": {"status": "passed"},
                "artifact_sha256": {
                    "component-height-catalogue.parquet": sha256_file(
                        tmp_path / "component-height-catalogue.parquet"
                    )
                },
            }
        )
    )
    assert validate_height_release(tmp_path)["blockers"] == []
