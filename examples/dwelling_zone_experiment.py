"""Dwelling-sized zoning sensitivity with the existing 10% unheated service core.

Uses bounded equal-area radial cuts around the service core. These are schematic
thermal zones, not inferred dwelling boundaries or reviewed apartment plans.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise
from pathlib import Path

from shapely import set_precision
from shapely.geometry import Polygon
from shapely.ops import unary_union

try:
    from .service_core_experiment import (
        choose_core,
        experiment_compiler,
        make_spec,
        opened_area_fraction,
        synthetic_cases,
    )
except ImportError:
    from service_core_experiment import (
        choose_core,
        experiment_compiler,
        make_spec,
        opened_area_fraction,
        synthetic_cases,
    )


def dwelling_chunks(
    footprint,
    core,
    *,
    target_m2=67.5,
    min_area_m2=55,
    max_area_m2=85,
    min_width_m=2.5,
    min_frontage_m=1.2,
    min_access_m=0.9,
):
    """Preserve all habitable area, with rounded count and bounded rotation search."""
    habitable = footprint.difference(core)
    count = max(1, math.floor(habitable.area / target_m2 + 0.5))
    area = habitable.area / count
    report = {
        "target_m2": target_m2,
        "habitable_m2": habitable.area,
        "requested_count": count,
        "equal_area_m2": area,
        "accepted": False,
    }
    if not min_area_m2 <= area <= max_area_m2:
        return {**report, "reason": "unit_area_outside_sensitivity_band"}, None
    if count < 2:
        return {**report, "reason": "no_subdivision_needed"}, None
    centre = core.representative_point()
    radius = footprint.length * 2

    def sector(a, b):
        steps = max(2, math.ceil((b - a) / (math.pi / 18)))
        return Polygon(
            [(centre.x, centre.y)]
            + [
                (
                    centre.x + radius * math.cos(a + (b - a) * j / steps),
                    centre.y + radius * math.sin(a + (b - a) * j / steps),
                )
                for j in range(steps + 1)
            ]
        )

    best = None
    # Stop at the first usable phase; no unconstrained layout optimisation.
    for degrees in (0, 10, 20, 30, 40, 50, 60, 70, 80, 90):
        start = math.radians(degrees)
        edges = [start]
        for _ in range(count - 1):
            lo, hi = edges[-1] + 1e-9, start + 2 * math.pi
            for _ in range(40):
                angle = (lo + hi) / 2
                if habitable.intersection(sector(edges[-1], angle)).area < area:
                    lo = angle
                else:
                    hi = angle
            edges.append((lo + hi) / 2)
        edges.append(start + 2 * math.pi)
        units = tuple(
            set_precision(habitable.intersection(sector(a, b)), 1e-8) for a, b in pairwise(edges)
        )
        if any(p.geom_type != "Polygon" or p.interiors or not p.is_valid for p in units):
            continue
        # Reject tiny corner remnants rather than repairing shared physical seams.
        if any(
            math.hypot(a[0] - b[0], a[1] - b[1]) < 0.010001
            for p in units
            for a, b in pairwise(p.exterior.coords)
        ):
            continue
        metrics = [
            {
                "area_m2": p.area,
                "width_retention": opened_area_fraction(p, min_width_m),
                "frontage_m": p.boundary.intersection(footprint.boundary).length,
                "access_m": p.boundary.intersection(core.boundary).length,
            }
            for p in units
        ]
        best = {**report, "phase_degrees": degrees, "units": metrics}
        if any(
            m["width_retention"] < 0.95
            or m["frontage_m"] < min_frontage_m
            or m["access_m"] < min_access_m
            for m in metrics
        ):
            continue
        coverage = unary_union([core, *units])
        assert coverage.symmetric_difference(footprint).area < footprint.area * 1e-6
        assert abs(sum(p.area for p in units) - habitable.area) < footprint.area * 1e-6
        return {**best, "accepted": True, "reason": "accepted"}, units
    return {**(best or report), "reason": "no_usable_chunks_in_bounded_search"}, None


def build_spec(name, footprint, core, units, scenario, epw, storeys=3):
    from epsm_core.pipeline import resolve_geometry
    from epsm_core.specs import BuildingPart, Footprint, GroundTemperatureSpec, ZoneUseSpec
    from epsm_core.zoning import PlanarZone

    regions = [PlanarZone(p, "perimeter") for p in units]
    regions.append(PlanarZone(core, "core"))
    _, reference_regions = choose_core(footprint)
    spec = make_spec(name, reference_regions, "unconditioned", epw)
    geometry = spec.geometry.updated(
        parts=tuple(
            BuildingPart(
                identifier="service_core" if region.kind == "core" else f"habitable_{j}",
                footprint=Footprint(
                    exterior=tuple(region.polygon.exterior.coords)[:-1],
                    holes=tuple(tuple(r.coords)[:-1] for r in region.polygon.interiors),
                ),
                storey_elevations_m=tuple(3 * i for i in range(storeys + 1)),
                conditioned=region.kind != "core",
            )
            for j, region in enumerate(regions)
        ),
        footprint_cleanup=spec.geometry.footprint_cleanup.updated(enabled=False),
    )
    spec = spec.updated(
        identifier=f"{name}_{scenario}",
        geometry=geometry,
        zone_uses=(),
        load_reference_area_m2=geometry.conditioned_floor_area_m2,
        envelope=spec.envelope.updated(
            ground_temperature=GroundTemperatureSpec(
                monthly_temperatures_c=(18.0,) * 12,
                source="energyplus_24_2_default_18C_uncalibrated_comparison_boundary",
            )
        ),
    )
    graph = resolve_geometry(spec).graph
    return spec.updated(
        zone_uses=tuple(
            ZoneUseSpec(
                zone_name=zone,
                use=spec.use,
                airflow=spec.airflow,
                reference_area_m2=data["floor_area_m2"],
                source="Identical occupied density, gains, DHW and ventilation across zone granularities",
            )
            for zone, data in graph.zones.items()
            if data["conditioned"]
        )
    )


def require_physical_partition_walls(model):
    """Require reciprocal interior wall faces with massive material constructions."""
    if model.get("Construction:AirBoundary"):
        raise ValueError("Dwelling and service-space models require physical partition walls")
    surfaces = model["BuildingSurface:Detailed"]
    walls = []
    for name, surface in surfaces.items():
        if surface["surface_type"] != "Wall" or surface["outside_boundary_condition"] != "Surface":
            continue
        adjacent = surfaces[surface["outside_boundary_condition_object"]]
        if adjacent["outside_boundary_condition_object"] != name:
            raise ValueError(f"Interior wall {name} has no reciprocal boundary")
        construction = model.get("Construction", {}).get(surface["construction_name"])
        if not construction:
            raise ValueError(f"Interior wall {name} has no physical construction")
        for material_name in construction.values():
            material = model.get("Material", {}).get(material_name)
            if not material or any(
                material[field] <= 0
                for field in ("thickness", "conductivity", "density", "specific_heat")
            ):
                raise ValueError(f"Interior wall {name} needs explicit massive material layers")
        walls.append(name)
    return len(walls) // 2


def solid_dwelling_compiler(energyplus, solid=True):
    """Compile physical dwelling/core walls; the generic assembly is an explicit assumption."""
    if not solid:
        raise ValueError("Dwelling and service-space models require physical partition walls")
    from epsm_core.compiler import CompilationReport
    from epsm_core.surface_checks import validate_surfaces

    compiler = experiment_compiler(energyplus)
    original_compile = compiler.compile
    compiler.compiler_hash = hashlib.sha256(
        (
            compiler.compiler_hash
            + str(solid)
            + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        ).encode()
    ).hexdigest()

    def compile_model(spec):
        model, report = original_compile(spec)
        data = report.data
        changed = []
        if solid:
            surfaces = model["BuildingSurface:Detailed"]
            for name, s in surfaces.items():
                if s["surface_type"] == "Wall" and s["construction_name"] == "partition":
                    s["construction_name"] = "experiment_service_partition"
                    changed.append(name)
                    data["surfaces"][name]["construction_name"] = "experiment_service_partition"
            if "Construction:AirBoundary" in model:
                del model["Construction:AirBoundary"]
        validate_surfaces(model, data["zones"])
        physical_wall_pairs = require_physical_partition_walls(model)
        errors = list(compiler.validator.iter_errors(model))
        if errors:
            raise ValueError(str(errors[0]))
        data["dwelling_experiment"] = {
            "solid_unit_partitions": solid,
            "physical_interior_wall_pairs": physical_wall_pairs,
            "partition_assumption": "150 mm concrete for the experiment; not an observed wall assembly",
            "changed_surfaces": changed,
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "scope": "Schematic thermal chunks; source footprint and total habitable volume preserved",
            "window_caveat": "Native WWR reapplied to new facade segments; opening positions can change",
        }
        return model, CompilationReport(
            **{
                **report.model_dump(),
                "run_id": hashlib.sha256(
                    json.dumps(
                        [compiler._input_identity(spec), model], sort_keys=True, allow_nan=False
                    ).encode()
                ).hexdigest(),
                "report_json": json.dumps(data, sort_keys=True),
            }
        )

    compiler.compile = compile_model
    return compiler


def simulate(name, footprint, core, units, scenario, args):
    import numpy as np
    from epsm_core.runner import EnergyPlusRunner

    spec = build_spec(name, footprint, core, units, scenario, args.epw)
    runner = EnergyPlusRunner(args.energyplus)
    if scenario == "chunks_virtual":
        raise ValueError("Dwelling and service-space models require physical partition walls")
    runner.compiler = solid_dwelling_compiler(args.energyplus)
    directory = args.output / name / scenario
    result = runner.run(spec, directory, timeout_seconds=300)
    model = json.loads((directory / "model.epJSON").read_text())
    data = json.loads(json.loads((directory / "compilation.json").read_text())["report_json"])
    occupied = [z for z, item in data["zones"].items() if item["conditioned"]]
    core_zones = [z for z, item in data["zones"].items() if not item["conditioned"]]

    def temperatures(zones):
        return np.asarray([result.hourly[f"Zone Mean Air Temperature::{z.upper()}"] for z in zones])

    air = temperatures(occupied)
    core_air = temperatures(core_zones)
    areas = np.asarray([data["zones"][z]["floor_area_m2"] for z in occupied])
    return {
        "case": name,
        "scenario": scenario,
        "accepted": result.diagnostics["accepted"],
        "warnings": result.diagnostics["warning_count"],
        "conditioned_zones": len(occupied),
        "core_zones": len(core_zones),
        "zone_area_min_m2": float(areas.min()),
        "zone_area_max_m2": float(areas.max()),
        "heating_kwh": result.scalars["space_heating_demand_kwh"],
        "dhw_kwh": result.scalars["dhw_load_kwh"],
        "occupied_hours_above26_area_weighted_percent": float(
            np.average(np.mean(air > 26, axis=1), weights=areas) * 100
        ),
        "occupied_temperature_max_c": float(air.max()),
        "core_temperature_min_c": float(core_air.min()),
        "conditioned_area_m2": sum(areas),
        "total_area_m2": sum(z["floor_area_m2"] for z in data["zones"].values()),
        "volume_m3": sum(z["volume_m3"] for z in data["zones"].values()),
        "people": sum(x["number_of_people"] for x in model.get("People", {}).values()),
        "equipment_w": sum(x["design_level"] for x in model.get("ElectricEquipment", {}).values()),
        "mean_outdoor_air_m3_s": sum(
            x.get("mean_outdoor_air_m3_s", 0) for x in data["zones"].values()
        ),
        "window_area_m2": sum(f["window_area_m2"] for f in data["facades"]),
        "artifact": str(directory),
    }


def plot(cases, output):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Polygon as PatchPolygon

    fig, axes = plt.subplots(2, 3, figsize=(13, 8), constrained_layout=True)
    colours = plt.get_cmap("Blues")
    for column, (name, (footprint, core, units, report)) in enumerate(cases.items()):
        for row, regions in enumerate(((footprint.difference(core),), units)):
            ax = axes[row, column]
            for i, p in enumerate(regions):
                ax.add_patch(
                    PatchPolygon(
                        p.exterior.coords,
                        facecolor=colours(0.3 + 0.5 * ((i % 4) / 3)),
                        edgecolor="white",
                    )
                )
                if row:
                    q = p.representative_point()
                    ax.text(q.x, q.y, f"{p.area:.1f}", ha="center", va="center", fontsize=8)
            ax.add_patch(PatchPolygon(core.exterior.coords, facecolor="#efb269", edgecolor="white"))
            x0, y0, x1, y1 = footprint.bounds
            ax.set_xlim(x0 - 2, x1 + 2)
            ax.set_ylim(y0 - 2, y1 + 2)
            ax.set_aspect("equal")
            ax.set_xlabel("metres")
            ax.set_ylabel("metres")
            ax.set_title(
                f"{footprint.area:.0f} m² floorplate: "
                + (
                    "one occupied zone"
                    if row == 0
                    else f"{len(units)} zones, {report['equal_area_m2']:.1f} m² each"
                )
            )
    fig.suptitle(
        "Dwelling-sized zones per storey; orange service core remains unheated\n"
        "Areas are schematic geometry, not inferred BOA or actual dwelling boundaries"
    )
    fig.savefig(output / "layouts.png", dpi=180)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--epw", required=True)
    parser.add_argument("--energyplus", required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if (args.output / "experiment-summary.json").exists():
        raise FileExistsError("Use a new directory")
    cases = {}
    for name in ["regular_400", "rectangular_450", "large_1200"]:
        footprint = synthetic_cases()[name]
        guard, regions = choose_core(footprint)
        assert guard["accepted"]
        core = next(r.polygon for r in regions if r.kind == "core")
        report, units = dwelling_chunks(footprint, core)
        assert report["accepted"], report
        cases[name] = (footprint, core, units, report)
    plot(cases, args.output)
    jobs = [(name, scenario) for name in cases for scenario in ["one_zone", "chunks_solid"]]

    def run(job):
        name, scenario = job
        footprint, core, units, _report = cases[name]
        if scenario == "one_zone":
            units = (footprint.difference(core),)
        try:
            result = simulate(name, footprint, core, units, scenario, args)
            print(json.dumps(result), flush=True)
            return result
        except Exception as error:  # noqa: BLE001 - retain every experiment failure
            print(f"FAILED {name}/{scenario}: {type(error).__name__}: {error}", flush=True)
            return {"case": name, "scenario": scenario, "accepted": False, "error": str(error)}

    with ThreadPoolExecutor(max_workers=2) as executor:
        simulations = list(executor.map(run, jobs))
    summary = {
        "source_statistics": "SCB housing stock 2025: mean MF dwelling BOA 67 m2, SF 122 m2; not medians.",
        "source_url": "https://www.scb.se/hitta-statistik/statistik-efter-amne/boende-bebyggelse-och-mark/bostader-och-boende/bostadsbestand/pong/statistiknyhet/bostadsbestandet-31-december-2025/",
        "storeys": 3,
        "target_m2": 67.5,
        "area_band_m2": [55, 85],
        "geometry": {name: values[3] for name, values in cases.items()},
        "simulations": simulations,
    }
    (args.output / "experiment-summary.json").write_text(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
