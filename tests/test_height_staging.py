import json

import pytest

from fixture_factory import build_height_staging_fixture
from sweden_ubem.config import validate_height_staging


def test_candidate_height_catalogue_is_verified_but_never_promotable(tmp_path):
    catalogue, register = build_height_staging_fixture(tmp_path)
    report = validate_height_staging(
        catalogue,
        expected_sha256=json.loads((catalogue.parent / "reconciliation.json").read_text())[
            "sha256"
        ][catalogue.name],
        geometry_register=register,
        evidence_path_map={"/tower/evidence": "/data/evidence"},
    )
    assert report["publication_status"] == "candidate"
    assert report["ubem_ready"] is False
    assert report["production_promotable"] is False
    assert report["component_rows"] == report["unique_component_ids"] == 3
    assert report["exact_baseline_records"] == 1
    assert report["selective_component_jobs_completed"] == 1
    assert report["awaiting_coverage_components"] == 1
    assert report["evidence_path_map"] == {"/tower/evidence": "/data/evidence"}


def test_candidate_height_catalogue_rejects_wrong_checksum(tmp_path):
    catalogue, register = build_height_staging_fixture(tmp_path)
    with pytest.raises(ValueError, match="expected-sha256"):
        validate_height_staging(catalogue, expected_sha256="0" * 64, geometry_register=register)
