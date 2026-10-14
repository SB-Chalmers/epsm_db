"""Check EPC-period renovation-state selection, including split-year periods."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/compare_uddevalla_renovation_matrix.py"
spec = spec_from_file_location("compare_uddevalla_renovation_matrix", SCRIPT)
module = module_from_spec(spec)
spec.loader.exec_module(module)


def test_best_state_sums_period_pieces_and_favors_less_intervention_on_ties():
    selected = [
        {"building_id": "one", "category": "multi_family", "era": "SE.01", "carrier": "district_heat",
         "regime": "after_v260", "period_start": "2020-07-01", "period_end_exclusive": "2021-07-01",
         "weather_years": [2020, 2021], "epc_reported_period_heating_kwh": 100.0,
         "model_period_heating_kwh": 160.0},
        {"building_id": "two", "category": "school", "era": "SE.02", "carrier": "district_heat",
         "regime": "after_v260", "period_start": "2021-01-01", "period_end_exclusive": "2022-01-01",
         "weather_years": [2021], "epc_reported_period_heating_kwh": 100.0,
         "model_period_heating_kwh": 90.0},
    ]
    runs = [
        {"building_id": "one", "renovation_state": state, "year": year,
         "status": "accepted", "period_piece_heating_kwh": piece}
        for state, pieces in (("standard", [45.0, 55.0]), ("advanced", [30.0, 40.0]))
        for year, piece in zip((2020, 2021), pieces)
    ] + [
        {"building_id": "two", "renovation_state": state, "year": 2021,
         "status": "accepted", "period_piece_heating_kwh": piece}
        for state, piece in (("standard", 110.0), ("advanced", 80.0))
    ]
    result = module.summarize(selected, runs)
    one, two = result["buildings"]
    assert one["standard_kwh"] == 100.0
    assert one["best_state"] == "standard"
    assert one["best_absolute_error_percent"] == 0.0
    assert two["best_state"] == "existing"  # 90 and 110 are equally close.
    assert result["overall"]["best_state_counts"] == {"existing": 1, "standard": 1, "advanced": 0}
    cell = next(r for r in result["matrix"] if r["category"] == "multi_family" and r["era"] == "SE.01")
    assert (cell["n"], cell["best_standard_count"], cell["best_median_absolute_error_percent"]) == (1, 1, 0.0)
