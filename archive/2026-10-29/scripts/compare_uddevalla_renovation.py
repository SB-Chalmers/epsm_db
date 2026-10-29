"""Paired renovation-package sensitivity for the Uddevalla EPC AMY panel.

The catalogue states are scenarios, not inferred renovation histories. Existing
AMY runs supply the frozen baseline and weather identity. Database reads only.
"""

import csv
import hashlib
import json
import os
from dataclasses import replace
from datetime import date
from pathlib import Path

from sweden_ubem.enums import BuildingType
from sweden_ubem.epc_heating import period_heating_kwh
from sweden_ubem.epsm import EPSMBuilding, ModelPolicy
from sweden_ubem.io import fetch_record
from sweden_ubem.records import implementation_hash
from sweden_ubem.weather import WeatherReference


ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / "output/analysis/uddevalla-consistency-2026-10-13"
BASE = STUDY / "amy_period_comparison"
OUT = BASE / "renovation_sensitivity_mf_se01"
STATES = ("standard", "advanced")
ENGINE = "/Applications/OpenStudio-3.9.0/EnergyPlus/energyplus"


def source_function_eligible(labels):
    return "Bostad;Flerfamiljshus" in labels and not any(
        label.startswith("Komplementbyggnad;") for label in labels
    )


def run_one(baseline, source, runs, state, year):
    bid = baseline["building_id"]
    old = runs[(bid, year)]
    weather = WeatherReference.from_epw(old["weather_epw"], calendar_year=year)
    if weather.sha256 != old["weather_sha256"]:
        raise ValueError("AMY weather checksum changed")
    row = {"building_id": bid, "renovation_state": state, "year": year,
           "weather_sha256": weather.sha256, "status": "error"}
    try:
        record = fetch_record.from_uuid(bid, dataset="uddevalla")
        if record.geometry.area < 100:
            raise ValueError("Footprint below 100 m²")
        if not record.selected_certificate or record.selected_certificate["certificate_id"] != old["certificate_id"]:
            raise ValueError("Selected EPC changed")
        if record.type != BuildingType.RESIDENTIAL_MULTIFAMILY:
            record = replace(record, type=BuildingType.RESIDENTIAL_MULTIFAMILY)
        overrides = {"renovation_state": state}
        if source["adapter_era"] != source["epc_era"]:
            overrides["construction_year"] = int(source["epc_year"])
        building, receipt = EPSMBuilding.from_record(
            record, weather=weather, policy=ModelPolicy(), overrides=overrides, energyplus=ENGINE
        )
        if building.spec.delivery.heating.carrier != baseline["carrier"]:
            raise ValueError("Heating carrier changed")
        if receipt.identities["archetype_assignment"]["components"]["wall"]["state"] != state:
            raise ValueError("Requested matrix state was not selected")
        directory = OUT / state / bid / str(year)
        directory.mkdir(parents=True, exist_ok=True)
        result = building.simulate(directory, energyplus=ENGINE, timeout_seconds=300)
        if not result.diagnostics["accepted"]:
            raise ValueError("EPSM simulation rejected")
        start = date.fromisoformat(baseline["period_start"])
        end = date.fromisoformat(baseline["period_end_exclusive"])
        row["period_piece_heating_kwh"] = period_heating_kwh(
            result.hourly["space_heating_carrier_input_kwh"], year, start, end
        )
        row["annual_heating_kwh"] = result.scalars["space_heating_carrier_input_kwh"]
        row["conditioned_area_m2"] = building.spec.geometry.conditioned_floor_area_m2
        row["load_reference_area_m2"] = building.spec.load_reference_area_m2
        row["matrix_assignment"] = receipt.identities["archetype_assignment"]
        row["status"] = "accepted"
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
    return row


def summarize(selected, runs):
    by_key = {(r["building_id"], r["renovation_state"], r["year"]): r for r in runs if r["status"] == "accepted"}
    rows = []
    for baseline in selected:
        bid = baseline["building_id"]
        years = baseline["weather_years"]
        row = {"building_id": bid, "carrier": baseline["carrier"],
               "period_start": baseline["period_start"], "period_end_exclusive": baseline["period_end_exclusive"],
               "epc_heating_kwh": baseline["epc_reported_period_heating_kwh"],
               "existing_kwh": baseline["model_period_heating_kwh"],
               "existing_ratio": baseline["model_over_epc_ratio"]}
        complete = True
        for state in STATES:
            if any((bid, state, year) not in by_key for year in years):
                complete = False
                break
            total = sum(by_key[(bid, state, year)]["period_piece_heating_kwh"] for year in years)
            row[state + "_kwh"] = total
            row[state + "_ratio"] = total / row["epc_heating_kwh"]
            row[state + "_versus_existing_percent"] = 100 * (total / row["existing_kwh"] - 1)
        if complete:
            rows.append(row)
    summary = {"buildings": rows, "complete": len(rows), "requested": len(selected)}
    if rows:
        import statistics

        summary["median_ratio"] = {state: statistics.median(row[state + "_ratio"] for row in rows)
                                   for state in ("existing",) + STATES}
        summary["median_absolute_percentage_error"] = {
            state: 100 * statistics.median(abs(row[state + "_ratio"] - 1) for row in rows)
            for state in ("existing",) + STATES
        }
        summary["aggregate_ratio"] = {
            state: sum(row[state + "_kwh"] for row in rows) / sum(row["epc_heating_kwh"] for row in rows)
            for state in ("existing",) + STATES
        }
    (OUT / "comparison.json").write_text(json.dumps(summary, indent=2) + "\n")
    if rows:
        with (OUT / "comparison.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=rows[0])
            writer.writeheader()
            writer.writerows(rows)
    return summary


def main():
    if not os.environ.get("UBEM_DATABASE_URL"):
        raise ValueError("Set read-only UBEM_DATABASE_URL")
    comparison = json.loads((BASE / "comparison.json").read_text())
    selected = [row for row in comparison["buildings"] if row["category"] == "multi_family" and row["era"] == "SE.01"]
    with (STUDY / "archetype_stock/building_assignments.csv").open(newline="") as stream:
        assignments = {row["building_id"]: row for row in csv.DictReader(stream)}
    for row in selected:
        source = assignments[row["building_id"]]
        if source["exclusion_reason"] or not source_function_eligible(json.loads(source["source_use_labels"])):
            raise ValueError("Source function no longer meets panel screen")
    baseline_runs = {(row["building_id"], row["year"]): row for row in map(json.loads, (BASE / "runs.jsonl").read_text().splitlines())}
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {"selection": "multi_family/SE.01 from screened AMY comparison, min 100 m² footprint and source function rechecked",
                "states": list(STATES), "weather": "Same checksum-checked AMY years as existing baseline",
                "target": "Same source-form reported-period delivered heating carrier input",
                "scenario_warning": "Renovation package selected hypothetically; no building renovation evidence inferred",
                "baseline_comparison_sha256": hashlib.sha256((BASE / "comparison.json").read_bytes()).hexdigest(),
                "baseline_runs_sha256": hashlib.sha256((BASE / "runs.jsonl").read_bytes()).hexdigest(),
                "implementation_sha256": {name: implementation_hash(name) for name in ("sweden_ubem", "epsm_core", "epsm_data")}}
    manifest_path = OUT / "manifest.json"
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text())
        if previous != manifest:
            raise ValueError("Existing renovation run has different inputs; choose a new output directory")
    else:
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    completed = [json.loads(line) for line in (OUT / "runs.jsonl").read_text().splitlines()] if (OUT / "runs.jsonl").exists() else []
    done = {(row["building_id"], row["renovation_state"], row["year"]) for row in completed if row["status"] == "accepted"}
    for baseline in selected:
        source = assignments[baseline["building_id"]]
        for state in STATES:
            for year in baseline["weather_years"]:
                key = (baseline["building_id"], state, year)
                if key in done:
                    continue
                result = run_one(baseline, source, baseline_runs, state, year)
                with (OUT / "runs.jsonl").open("a") as stream:
                    stream.write(json.dumps(result, default=str) + "\n")
                print(state, year, baseline["building_id"], result["status"], result.get("error", ""), flush=True)
    completed = [json.loads(line) for line in (OUT / "runs.jsonl").read_text().splitlines()]
    print(json.dumps(summarize(selected, completed), indent=2), flush=True)


if __name__ == "__main__":
    main()
