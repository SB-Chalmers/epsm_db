from sweden_ubem.comparison import compare_relations
import gzip
import json
import pytest
from scripts.classify_parity import classify


@pytest.mark.parametrize("area,accepted", [(100, True), (101, False)])
def test_only_exact_authoritative_atemp_delta_is_accepted(tmp_path, area, accepted):
    old = {
        "certificate_id": "efter_v260:1",
        "source_field": "EgenAtemp",
        "observation_id": "1",
        "raw_values": ["100"],
        "mapping_status": "unverified",
        "value_status": "unverified",
        "normalized_value": None,
    }
    new = {
        **old,
        "mapping_status": "verified",
        "value_status": "available",
        "normalized_value": {"value": area},
    }
    counts = {"missing_rows": 1, "added_rows": 1, "added_columns": [], "removed_columns": []}
    (tmp_path / "report.json").write_text(
        json.dumps(
            {"equal_functional_values": False, "functional_views": {"epc_observations": counts}}
        )
    )
    with gzip.open(tmp_path / "epc_observations-differences.jsonl.gz", "wt") as f:
        f.write(json.dumps({"row": old, "baseline_count": 1, "current_count": 0}) + "\n")
        f.write(json.dumps({"row": new, "baseline_count": 0, "current_count": 1}) + "\n")
    result = classify(tmp_path)
    assert result["accepted"] == accepted
    assert result["equal_functional_values"] is False


def test_oracle_difference_reports_unresolved_without_importing_links():
    row = dict(
        building_id="b",
        formular_id=1,
        municipality_code="1485",
        designation=" ÅBY  1:1 ",
        house_number=1,
        decision="accepted",
    )
    result = compare_relations(
        [], [row], {"b": {"resolution_status": "unresolved", "municipality_code": "1485"}}
    )
    assert result[0]["reason"] == "current_geometry_unresolved"
    assert result[0]["diagnostic_status"] == "classified"
    assert compare_relations([row], [{**row, "designation": "åby 1:1"}], {}) == []


def test_oracle_unexplained_difference_is_recorded_without_review_queue():
    row = dict(
        building_id="b",
        formular_id=1,
        municipality_code="1485",
        designation="A 1:1",
        house_number=1,
        decision="accepted",
    )
    result = compare_relations(
        [], [row], {"b": {"resolution_status": "resolved", "municipality_code": "1485"}}
    )
    assert result[0]["diagnostic_status"] == "unclassified"
