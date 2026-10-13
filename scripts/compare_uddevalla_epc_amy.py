"""Reproduce period-matched EPC heating comparisons on the screened Uddevalla panel.

Requires read-only UBEM_DATABASE_URL, smhi2epw, EPSM Core and EnergyPlus. Each
calendar AMY is compiled once at the documented Uddevalla reference point; a
cross-year EPC interval uses the correct months of two separate simulations.
The JSONL is append-only and already completed building/year runs are resumed.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import statistics
from dataclasses import replace
from datetime import date
from pathlib import Path

from sweden_ubem.enums import BuildingType
from sweden_ubem.epc_heating import heating_target_from_record, period_heating_kwh
from sweden_ubem.epsm import EPSMBuilding, ModelPolicy
from sweden_ubem.io import fetch_record
from sweden_ubem.records import implementation_hash
from sweden_ubem.weather import WeatherReference


ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / "output/analysis/uddevalla-consistency-2026-10-13"
BASELINE = STUDY / "baseline_policy_v3_panel/results.jsonl"
ASSIGNMENTS = STUDY / "archetype_stock/building_assignments.csv"
OUT = STUDY / "amy_period_comparison"
ENGINE = "/Applications/OpenStudio-3.9.0/EnergyPlus/energyplus"
MIN_FOOTPRINT_M2 = 100.0
REFERENCE_POINT = {"latitude": 58.37124111847143, "longitude": 11.938648025184055}


def _source_function_eligible(category, labels):
    if any(label.startswith("Komplementbyggnad;") for label in labels):
        return False
    return {
        "single_family": bool(set(labels) & {"Bostad;Småhus friliggande", "Bostad;Småhus kedjehus", "Bostad;Småhus radhus"}),
        "multi_family": "Bostad;Flerfamiljshus" in labels,
        "school": bool(set(labels) & {"Samhallsfunktion;Skola", "Samhällsfunktion;Skola"}),
        "office": any(label.startswith("Verksamhet;") for label in labels),
    }.get(category, False)


def _distance_km(point):
    a, b = math.radians(point["latitude"]), math.radians(REFERENCE_POINT["latitude"])
    da = a - b
    db = math.radians(point["longitude"] - REFERENCE_POINT["longitude"])
    return 12742 * math.asin(math.sqrt(math.sin(da / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(db / 2) ** 2))


def _records():
    base = {row["building_id"]: row for line in BASELINE.read_text().splitlines() if (row := json.loads(line)).get("success")}
    with ASSIGNMENTS.open(newline="") as stream:
        assignments = {row["building_id"]: row for row in csv.DictReader(stream)}
    selected = []
    for bid, old in base.items():
        source = assignments[bid]
        labels = json.loads(source["source_use_labels"])
        if source["exclusion_reason"] or not _source_function_eligible(old["category"], labels):
            continue
        if (old.get("footprint_m2") or 0) < MIN_FOOTPRINT_M2:
            continue
        selected.append((old, source))
    return selected


def _weather(year):
    return WeatherReference.from_location(
        **REFERENCE_POINT, year=year, directory=OUT / "weather"
    )


def _one(old, source, weather):
    bid, year = old["building_id"], weather.calendar_year
    directory = OUT / "simulation" / bid / str(year)
    row = {"building_id": bid, "year": year, "category": old["category"],
           "era": old["era"], "certificate_id": old["certificate_id"],
           "weather_sha256": weather.sha256, "weather_epw": weather.epw_path,
           "status": "error"}
    try:
        record = fetch_record.from_uuid(bid, dataset="uddevalla")
        if record.selected_certificate is None or record.selected_certificate["certificate_id"] != old["certificate_id"]:
            raise ValueError("Selected EPC changed relative to the baseline snapshot")
        if record.geometry.area < MIN_FOOTPRINT_M2:
            raise ValueError("Footprint no longer passes minimum area")
        row["weather_distance_km"] = _distance_km(record.location)
        if record.type.value != old["category"]:
            record = replace(record, type=BuildingType(old["category"]))
        target = heating_target_from_record(record)
        row["target"] = target.as_dict()
        if target.reported_period_heating_kwh is None:
            raise ValueError(f"No comparable reported-period target: {target.exclusions}")
        overrides = {}
        if source["adapter_era"] != source["epc_era"]:
            overrides["construction_year"] = int(source["epc_year"])
        building, receipt = EPSMBuilding.from_record(
            record, weather=weather, policy=ModelPolicy(), overrides=overrides, energyplus=ENGINE
        )
        row["model_carrier"] = building.spec.delivery.heating.carrier if building.spec.delivery.heating else None
        if row["model_carrier"] != target.carrier:
            raise ValueError("EPC carrier and model carrier differ")
        directory.mkdir(parents=True, exist_ok=True)
        result = building.simulate(directory, energyplus=ENGINE, timeout_seconds=300)
        if not result.diagnostics["accepted"]:
            raise ValueError("EPSM Core rejected simulation")
        row["annual_model_heating_kwh"] = result.scalars["space_heating_carrier_input_kwh"]
        row["period_piece_model_heating_kwh"] = period_heating_kwh(
            result.hourly["space_heating_carrier_input_kwh"], year,
            target.period_start, target.period_end_exclusive,
        )
        row["status"] = "accepted"
        row["receipt_quality"] = receipt.quality
    except Exception as error:
        row["error"] = f"{type(error).__name__}: {error}"
    return row


def _summarize(rows):
    by_bid = {}
    for row in rows:
        by_bid.setdefault(row["building_id"], {})[row["year"]] = row
    comparisons = []
    for old, _ in _records():
        bid = old["building_id"]
        parts = by_bid.get(bid, {})
        available = [row for row in parts.values() if row["status"] == "accepted"]
        if not available:
            continue
        target = available[0]["target"]
        start, end = date.fromisoformat(target["period_start"]), date.fromisoformat(target["period_end_exclusive"])
        required = tuple(range(start.year, end.year + (end.month > 1)))
        if any(year not in parts or parts[year]["status"] != "accepted" for year in required):
            continue
        modeled = sum(parts[year]["period_piece_model_heating_kwh"] for year in required)
        observed = target["reported_period_heating_kwh"]
        comparisons.append({
            "building_id": bid, "category": old["category"], "era": old["era"],
            "archetype": f"{old['category']}/{old['era']}",
            "carrier": target["carrier"], "regime": target["regime"],
            "period_start": target["period_start"], "period_end_exclusive": target["period_end_exclusive"],
            "weather_years": required, "weather_distance_km": max(parts[y]["weather_distance_km"] for y in required),
            "epc_reported_period_heating_kwh": observed,
            "epc_inferred_normal_year_heating_kwh": target["inferred_normal_year_heating_kwh"],
            "model_period_heating_kwh": modeled,
            "model_over_epc_ratio": modeled / observed,
            "model_minus_epc_kwh": modeled - observed,
            "tmy_model_heating_kwh": old["scalars"].get("space_heating_carrier_input_kwh"),
            "tmy_over_same_epc_ratio": old["scalars"].get("space_heating_carrier_input_kwh") / observed,
            "amy_vs_tmy_model_change_percent": 100 * (modeled / old["scalars"]["space_heating_carrier_input_kwh"] - 1),
        })
    if comparisons:
        ratios = [row["model_over_epc_ratio"] for row in comparisons]
        nearby = [row for row in comparisons if row["weather_distance_km"] <= 10]
        direct = [row for row in comparisons if row["carrier"] in ("district_heat", "electric_direct")]
        overall = {"count": len(comparisons), "median_ratio": statistics.median(ratios),
                   "median_absolute_percentage_error": 100 * statistics.median(abs(value - 1) for value in ratios),
                   "within_25_percent_count": sum(.75 <= value <= 1.25 for value in ratios),
                   "within_50_percent_count": sum(.5 <= value <= 1.5 for value in ratios),
                   "aggregate_ratio": sum(row["model_period_heating_kwh"] for row in comparisons) / sum(row["epc_reported_period_heating_kwh"] for row in comparisons),
                   "tmy_median_ratio_to_same_target": statistics.median(row["tmy_over_same_epc_ratio"] for row in comparisons),
                   "tmy_aggregate_ratio_to_same_target": sum(row["tmy_model_heating_kwh"] for row in comparisons) / sum(row["epc_reported_period_heating_kwh"] for row in comparisons),
                   "median_amy_vs_tmy_model_change_percent": statistics.median(row["amy_vs_tmy_model_change_percent"] for row in comparisons),
                   "below_half": sum(value < .5 for value in ratios), "above_two": sum(value > 2 for value in ratios),
                   "max_weather_distance_km": max(row["weather_distance_km"] for row in comparisons),
                   "median_weather_distance_km": statistics.median(row["weather_distance_km"] for row in comparisons),
                   "within_10km_count": len(nearby),
                   "within_10km_median_ratio": statistics.median(row["model_over_epc_ratio"] for row in nearby) if nearby else None,
                   "direct_carrier_count": len(direct),
                   "direct_carrier_median_ratio": statistics.median(row["model_over_epc_ratio"] for row in direct) if direct else None}
    else:
        overall = {"count": 0}
    OUT.mkdir(parents=True, exist_ok=True)
    groups = {}
    for field in ("carrier", "category", "regime", "archetype"):
        groups[field] = {}
        for name in sorted({row[field] for row in comparisons}):
            subset = [row for row in comparisons if row[field] == name]
            groups[field][name] = {
                "count": len(subset),
                "median_amy_over_epc": statistics.median(row["model_over_epc_ratio"] for row in subset),
                "median_tmy_over_same_epc": statistics.median(row["tmy_over_same_epc_ratio"] for row in subset),
            }
    weather_quality = {}
    for sidecar in (OUT / "weather").glob("*/weather.epw.json"):
        item = json.loads(sidecar.read_text())
        result = item["result"]
        weather_quality[item["configuration"]["year"]] = {
            "interpolated_fraction": result.get("interpolated_fraction"),
            "station_distance_km": result.get("coordinate_distance_km"),
            "solar_source": result.get("report", {}).get("solar_source"),
            "provenance_path": str(sidecar),
        }
    (OUT / "comparison.json").write_text(json.dumps({"overall": overall, "groups": groups, "weather_quality": weather_quality, "buildings": comparisons}, indent=2, default=str) + "\n")
    if comparisons:
        with (OUT / "comparison.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=comparisons[0].keys())
            writer.writeheader()
            writer.writerows(comparisons)
    return overall


def main():
    if not os.environ.get("UBEM_DATABASE_URL"):
        raise ValueError("Set read-only UBEM_DATABASE_URL")
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {"baseline_sha256": hashlib.sha256(BASELINE.read_bytes()).hexdigest(),
                "assignments_sha256": hashlib.sha256(ASSIGNMENTS.read_bytes()).hexdigest(),
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "implementation_sha256": {name: implementation_hash(name) for name in ("sweden_ubem", "epsm_core", "epsm_data", "smhi2epw")},
                "reference_point": REFERENCE_POINT, "minimum_footprint_m2": MIN_FOOTPRINT_M2,
                "target": "reported delivered space-heating input over exact EPC measurement months; normal-year inference retained separately",
                "weather": "smhi2epw calendar AMYs at documented Uddevalla reference point; site distance recorded; source quality in EPW sidecars",
                "no_calibration": True}
    path = OUT / "manifest.json"
    if path.exists():
        old = json.loads(path.read_text())
        for key in ("baseline_sha256", "assignments_sha256", "reference_point", "minimum_footprint_m2"):
            if old[key] != manifest[key]:
                raise ValueError(f"Resume input changed: {key}")
    else:
        path.write_text(json.dumps(manifest, indent=2) + "\n")
    selected = _records()
    completed = [json.loads(line) for line in (OUT / "runs.jsonl").read_text().splitlines()] if (OUT / "runs.jsonl").exists() else []
    done = {(row["building_id"], row["year"]) for row in completed if row["status"] == "accepted"}
    # Use raw baseline EPC evidence to plan years without making extra DB calls.
    from sweden_ubem.epc_heating import heating_target_from_raw
    tasks = []
    for old, source in selected:
        target = heating_target_from_raw(old["certificate_id"], old.get("epc_energy_raw_values") or {})
        if target.reported_period_heating_kwh is None or target.carrier != (old.get("diagnostics") or {}).get("heating_delivery", {}).get("carrier"):
            continue
        tasks.extend((year, old, source) for year in target.years if (old["building_id"], year) not in done)
    print(f"selected={len(selected)} already_accepted={len(done)} pending_building_years={len(tasks)}", flush=True)
    for year in sorted({year for year, _, _ in tasks}):
        weather = _weather(year)
        for _, old, source in (task for task in tasks if task[0] == year):
            result = _one(old, source, weather)
            with (OUT / "runs.jsonl").open("a") as stream:
                stream.write(json.dumps(result, default=str) + "\n")
            print(f"{year} {old['building_id']} {result['status']} {result.get('error', '')}", flush=True)
    all_runs = [json.loads(line) for line in (OUT / "runs.jsonl").read_text().splitlines()] if (OUT / "runs.jsonl").exists() else []
    print(json.dumps(_summarize(all_runs)), flush=True)


if __name__ == "__main__":
    main()
