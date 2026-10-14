"""Run every catalogue renovation state on the screened Uddevalla AMY/EPC panel.

The EPC is used to select the closest state, so the selected error is an
in-sample diagnostic fit, not validation or evidence of renovation history.
Database access is read-only. Accepted building/year runs are resumable.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
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
OUT = BASE / "renovation_matrix_all"
PREVIOUS = BASE / "renovation_sensitivity_mf_se01/runs.jsonl"
ASSIGNMENTS = STUDY / "archetype_stock/building_assignments.csv"
PANEL = STUDY / "baseline_policy_v3_panel/results.jsonl"
ENGINE = "/Applications/OpenStudio-3.9.0/EnergyPlus/energyplus"
STATES = ("existing", "standard", "advanced")
RENOVATED = STATES[1:]
MIN_FOOTPRINT_M2 = 100.0


def eligible(category: str, labels: list[str]) -> bool:
    if any(label.startswith("Komplementbyggnad;") for label in labels):
        return False
    return {
        "single_family": bool(set(labels) & {"Bostad;Småhus friliggande", "Bostad;Småhus kedjehus", "Bostad;Småhus radhus"}),
        "multi_family": "Bostad;Flerfamiljshus" in labels,
        "school": bool(set(labels) & {"Samhallsfunktion;Skola", "Samhällsfunktion;Skola"}),
        "office": any(label.startswith("Verksamhet;") for label in labels),
    }.get(category, False)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def prepare():
    selected = json.loads((BASE / "comparison.json").read_text())["buildings"]
    with ASSIGNMENTS.open(newline="") as stream:
        sources = {row["building_id"]: row for row in csv.DictReader(stream)}
    panel = {row["building_id"]: row for row in read_jsonl(PANEL) if row.get("success")}
    baseline_runs = {(row["building_id"], row["year"]): row for row in read_jsonl(BASE / "runs.jsonl") if row["status"] == "accepted"}
    for row in selected:
        bid = row["building_id"]
        source = sources[bid]
        if source["exclusion_reason"] or not eligible(row["category"], json.loads(source["source_use_labels"])):
            raise ValueError(f"Source function or exclusion screen changed: {bid}")
        if panel[bid]["footprint_m2"] < MIN_FOOTPRINT_M2:
            raise ValueError(f"Footprint below {MIN_FOOTPRINT_M2} m²: {bid}")
        if row["epc_reported_period_heating_kwh"] <= 0:
            raise ValueError(f"Nonpositive EPC heating: {bid}")
        for year in row["weather_years"]:
            if (bid, year) not in baseline_runs:
                raise ValueError(f"Missing accepted baseline: {bid} {year}")
    return selected, sources, baseline_runs


def run_one(baseline: dict, source: dict, old: dict, state: str) -> dict:
    bid, year = baseline["building_id"], old["year"]
    row = {"building_id": bid, "renovation_state": state, "year": year,
           "weather_sha256": old["weather_sha256"], "status": "error"}
    try:
        weather = WeatherReference.from_epw(old["weather_epw"], calendar_year=year)
        if weather.sha256 != old["weather_sha256"]:
            raise ValueError("AMY weather checksum changed")
        record = fetch_record.from_uuid(bid, dataset="uddevalla")
        if record.geometry.area < MIN_FOOTPRINT_M2:
            raise ValueError("Footprint below minimum area")
        if not record.selected_certificate or record.selected_certificate["certificate_id"] != old["certificate_id"]:
            raise ValueError("Selected EPC changed")
        if record.type.value != baseline["category"]:
            record = replace(record, type=BuildingType(baseline["category"]))
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
        directory = OUT / "simulation" / state / bid / str(year)
        directory.mkdir(parents=True, exist_ok=True)
        result = building.simulate(directory, energyplus=ENGINE, timeout_seconds=300)
        if not result.diagnostics["accepted"]:
            raise ValueError("EPSM simulation rejected")
        row["period_piece_heating_kwh"] = period_heating_kwh(
            result.hourly["space_heating_carrier_input_kwh"], year,
            date.fromisoformat(baseline["period_start"]),
            date.fromisoformat(baseline["period_end_exclusive"]),
        )
        row["annual_heating_kwh"] = result.scalars["space_heating_carrier_input_kwh"]
        row["conditioned_area_m2"] = building.spec.geometry.conditioned_floor_area_m2
        row["load_reference_area_m2"] = building.spec.load_reference_area_m2
        row["matrix_assignment"] = receipt.identities["archetype_assignment"]
        row["status"] = "accepted"
    except Exception as error:
        row["error"] = f"{type(error).__name__}: {error}"
    return row


def summarize(selected: list[dict], runs: list[dict]) -> dict:
    accepted = {(r["building_id"], r["renovation_state"], r["year"]): r
                for r in runs if r["status"] == "accepted"}
    buildings = []
    for baseline in selected:
        bid, years = baseline["building_id"], baseline["weather_years"]
        if any((bid, state, year) not in accepted for state in RENOVATED for year in years):
            continue
        observed = baseline["epc_reported_period_heating_kwh"]
        row = {key: baseline[key] for key in ("building_id", "category", "era", "carrier", "regime", "period_start", "period_end_exclusive")}
        row["epc_heating_kwh"] = observed
        row["weather_years"] = years
        for state in STATES:
            modeled = (baseline["model_period_heating_kwh"] if state == "existing" else
                       sum(accepted[bid, state, year]["period_piece_heating_kwh"] for year in years))
            row[f"{state}_kwh"] = modeled
            row[f"{state}_ratio"] = modeled / observed
            row[f"{state}_absolute_error_percent"] = 100 * abs(modeled / observed - 1)
        best = min(STATES, key=lambda state: row[f"{state}_absolute_error_percent"])
        row["best_state"] = best
        row["best_ratio"] = row[f"{best}_ratio"]
        row["best_absolute_error_percent"] = row[f"{best}_absolute_error_percent"]
        row["error_reduction_percentage_points"] = row["existing_absolute_error_percent"] - row["best_absolute_error_percent"]
        buildings.append(row)
    matrix = []
    for category in ("single_family", "multi_family", "school", "office"):
        for era in ("SE.01", "SE.02", "SE.03", "SE.04", "SE.05", "SE.06"):
            group = [r for r in buildings if r["category"] == category and r["era"] == era]
            cell = {"category": category, "era": era, "n": len(group)}
            for state in STATES:
                cell[f"best_{state}_count"] = sum(r["best_state"] == state for r in group)
                cell[f"{state}_median_absolute_error_percent"] = (statistics.median(r[f"{state}_absolute_error_percent"] for r in group) if group else None)
            cell["best_median_absolute_error_percent"] = (statistics.median(r["best_absolute_error_percent"] for r in group) if group else None)
            matrix.append(cell)
    overall = {"requested_buildings": len(selected), "completed_buildings": len(buildings),
               "best_state_counts": {state: sum(r["best_state"] == state for r in buildings) for state in STATES},
               "median_absolute_error_percent": {state: statistics.median(r[f"{state}_absolute_error_percent"] for r in buildings) if buildings else None for state in STATES},
               "best_median_absolute_error_percent": statistics.median(r["best_absolute_error_percent"] for r in buildings) if buildings else None,
               "within_25_percent": {state: sum(r[f"{state}_absolute_error_percent"] <= 25 for r in buildings) for state in STATES},
               "best_within_25_percent": sum(r["best_absolute_error_percent"] <= 25 for r in buildings)}
    return {"overall": overall, "matrix": matrix, "buildings": buildings}


def save_summary(summary: dict):
    (OUT / "comparison.json").write_text(json.dumps(summary, indent=2) + "\n")
    for name, rows in (("buildings", summary["buildings"]), ("matrix", summary["matrix"])):
        if rows:
            with (OUT / f"{name}.csv").open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)


def plot_matrix(matrix: list[dict]):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch, Rectangle

    categories = ("single_family", "multi_family", "school", "office")
    eras = ("SE.01", "SE.02", "SE.03", "SE.04", "SE.05", "SE.06")
    colors = {"existing": "#47729b", "standard": "#db9850", "advanced": "#67a27e"}
    cells = {(row["category"], row["era"]): row for row in matrix}
    fig, ax = plt.subplots(figsize=(12, 6.2))
    for i, category in enumerate(categories):
        for j, era in enumerate(eras):
            row = cells[category, era]
            n = row["n"]
            if n:
                dominant = max(STATES, key=lambda state: row[f"best_{state}_count"])
                color = colors[dominant]
                counts = "/".join(str(row[f"best_{state}_count"]) for state in STATES)
                label = f"n={n}  E/S/A={counts}\nmedian best error {row['best_median_absolute_error_percent']:.0f}%"
            else:
                color, label = "#eeeeee", "n=0"
            ax.add_patch(Rectangle((j, i), 1, 1, facecolor=color, edgecolor="white", linewidth=3))
            ax.text(j + .5, i + .5, label, ha="center", va="center", color="white" if n else "#333333", fontsize=8.5)
    ax.set(xlim=(0, len(eras)), ylim=(len(categories), 0), title="Uddevalla: closest renovation state by archetype cell")
    ax.set_xticks([j + .5 for j in range(len(eras))], eras)
    ax.set_yticks([i + .5 for i in range(len(categories))], ["Single family", "Multifamily", "School", "Office"])
    ax.tick_params(length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.legend(handles=[Patch(facecolor=colors[state], label=f"{state.title()} most frequent") for state in STATES],
              loc="upper center", bbox_to_anchor=(.5, -.04), ncol=3, frameon=False)
    fig.text(.5, .015, "E/S/A = count closest to EPC for existing / standard / advanced. In-sample fit; EPC does not establish renovation history.",
             ha="center", fontsize=8, color="#444444")
    fig.tight_layout(rect=(0, .07, 1, 1))
    fig.savefig(OUT / "best_state_matrix.png", dpi=180)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summarize-only", action="store_true")
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    if not args.summarize_only and not os.environ.get("UBEM_DATABASE_URL"):
        raise ValueError("Set read-only UBEM_DATABASE_URL")
    selected, sources, baseline_runs = prepare()
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {
        "selection": "All comparable buildings from the screened AMY panel; source function, exclusions and 100 m² minimum footprint rechecked",
        "renovation_states": list(STATES),
        "weather": "Same checksum-checked building/calendar-year AMYs as existing baseline",
        "target": "Same source-form reported-period delivered heating carrier input",
        "selection_rule": "Minimum absolute percentage error against the same EPC target; ties favor less intervention",
        "interpretation": "In-sample diagnostic oracle, not independent validation or evidence of actual renovation",
        "baseline_comparison_sha256": hashlib.sha256((BASE / "comparison.json").read_bytes()).hexdigest(),
        "baseline_runs_sha256": hashlib.sha256((BASE / "runs.jsonl").read_bytes()).hexdigest(),
        "assignments_sha256": hashlib.sha256(ASSIGNMENTS.read_bytes()).hexdigest(),
        "implementation_sha256": {name: implementation_hash(name) for name in ("sweden_ubem", "epsm_core", "epsm_data")},
    }
    path = OUT / "manifest.json"
    if path.exists():
        if json.loads(path.read_text()) != manifest:
            raise ValueError("Resume inputs changed; choose a new output directory")
    else:
        path.write_text(json.dumps(manifest, indent=2) + "\n")
    runs_path = OUT / "runs.jsonl"
    runs = read_jsonl(runs_path)
    done = {(r["building_id"], r["renovation_state"], r["year"]) for r in runs if r["status"] == "accepted"}
    # Reuse the previously completed MF/SE.01 runs only with matching AMY identity.
    if not args.summarize_only:
        selected_ids = {r["building_id"] for r in selected}
        with runs_path.open("a") as stream:
            for r in read_jsonl(PREVIOUS):
                key = (r["building_id"], r["renovation_state"], r["year"])
                if r["status"] != "accepted" or r["building_id"] not in selected_ids or key in done:
                    continue
                if r["weather_sha256"] != baseline_runs[r["building_id"], r["year"]]["weather_sha256"]:
                    raise ValueError("Previously run AMY weather checksum differs")
                copied = {**r, "reused_from": str(PREVIOUS.relative_to(ROOT))}
                stream.write(json.dumps(copied) + "\n")
                runs.append(copied)
                done.add(key)
        tasks = [(baseline, sources[baseline["building_id"]], baseline_runs[baseline["building_id"], year], state)
                 for baseline in selected for state in RENOVATED for year in baseline["weather_years"]
                 if (baseline["building_id"], state, year) not in done]
        print(f"selected={len(selected)} reused_or_resumed={len(done)} pending={len(tasks)}", flush=True)
        if tasks:
            # Fail before scheduling the panel when local database access is denied.
            fetch_record.from_uuid(tasks[0][0]["building_id"], dataset="uddevalla")
        with ThreadPoolExecutor(max_workers=args.workers) as pool, runs_path.open("a") as stream:
            futures = [pool.submit(run_one, *task) for task in tasks]
            for i, future in enumerate(as_completed(futures), 1):
                result = future.result()
                stream.write(json.dumps(result, default=str) + "\n")
                stream.flush()
                runs.append(result)
                print(f"{i}/{len(tasks)} {result['renovation_state']} {result['year']} {result['building_id']} {result['status']} {result.get('error', '')}", flush=True)
    summary = summarize(selected, runs)
    save_summary(summary)
    plot_matrix(summary["matrix"])
    print(json.dumps(summary["overall"], indent=2), flush=True)


if __name__ == "__main__":
    main()
