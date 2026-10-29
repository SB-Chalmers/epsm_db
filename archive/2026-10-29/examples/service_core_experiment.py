"""Small, isolated service-core sensitivity experiment; not an adapter default.

Geometry guards are schematic usability screens, not regulatory room-layout checks.
Run with --output, --epw and --energyplus. Existing stored footprints can be supplied
with --sample. The optional simulation compiler explicitly replaces virtual core
boundaries with a declared solid partition, without editing EPSM Core itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from shapely import from_wkt
from shapely.geometry import Polygon, box
from shapely.ops import unary_union


@dataclass(frozen=True)
class CorePolicy:
    service_fraction: float = 0.10
    min_footprint_m2: float = 150
    min_habitable_depth_m: float = 2.5
    min_core_area_m2: float = 12
    min_core_width_m: float = 2
    minimum_width_area_retention: float = 0.95


def opened_area_fraction(polygon, width):
    """Check for substantial thin necks/appendages using a mitred morphological opening."""
    inset = polygon.buffer(-width / 2, join_style="mitre")
    if inset.is_empty:
        return 0.0
    restored = inset.buffer(width / 2, join_style="mitre").intersection(polygon)
    return restored.area / polygon.area


def choose_core(polygon, policy=None):
    from epsm_core.geometry import facade_edges
    from epsm_core.zoning import perimeter_core_partitions

    policy = policy or CorePolicy()
    if not 0 < policy.service_fraction < 1:
        raise ValueError("service_fraction must lie strictly between zero and one")
    report = {"footprint_m2": polygon.area, "accepted": False, "reason": None}
    if polygon.geom_type != "Polygon" or not polygon.is_valid or polygon.is_empty:
        report["reason"] = "invalid_or_multiple_footprints"
        return report, None
    if polygon.area < policy.min_footprint_m2:
        report["reason"] = "small_footprint"
        return report, None
    target = polygon.area * policy.service_fraction
    report["target_core_m2"] = target
    if target < policy.min_core_area_m2:
        report["reason"] = "small_service_area"
        return report, None
    # An inset gives a cheap first estimate; exact Core partitions verify the area.
    low, high = (
        0.0,
        max(polygon.bounds[2] - polygon.bounds[0], polygon.bounds[3] - polygon.bounds[1]),
    )
    for _ in range(35):
        depth = (low + high) / 2
        if polygon.buffer(-depth, join_style="mitre").area > target:
            low = depth
        else:
            high = depth
    depth = (low + high) / 2
    report["perimeter_depth_m"] = depth
    if depth < policy.min_habitable_depth_m:
        report["reason"] = "shallow_habitable_band"
        return report, None
    try:
        partitions = perimeter_core_partitions(polygon, list(facade_edges(polygon, "main")), depth)
    except ValueError as error:
        report.update(reason="partition_failure", error=str(error))
        return report, None
    cores = [region.polygon for region in partitions if region.kind == "core"]
    if len(cores) != 1 or cores[0].interiors:
        report["reason"] = "fragmented_or_holed_core"
        return report, None
    core = cores[0]
    occupied = polygon.difference(core)
    report.update(
        actual_core_m2=core.area,
        actual_service_fraction=core.area / polygon.area,
        core_width_area_retention=opened_area_fraction(core, policy.min_core_width_m),
        habitable_width_area_retention=opened_area_fraction(occupied, policy.min_habitable_depth_m),
    )
    if abs(core.area - target) / target > 0.01:
        report["reason"] = "target_area_not_reproduced"
    elif core.area < policy.min_core_area_m2:
        report["reason"] = "small_service_area"
    elif report["core_width_area_retention"] < policy.minimum_width_area_retention:
        report["reason"] = "narrow_service_core"
    elif report["habitable_width_area_retention"] < policy.minimum_width_area_retention:
        report["reason"] = "narrow_habitable_space"
    elif occupied.geom_type != "Polygon":
        report["reason"] = "disconnected_habitable_space"
    else:
        coverage = unary_union([region.polygon for region in partitions])
        assert coverage.symmetric_difference(polygon).area < polygon.area * 1e-6
        assert (
            abs(sum(region.polygon.area for region in partitions) - polygon.area)
            < 1e-6 * polygon.area
        )
        report.update(accepted=True, reason="accepted")
        return report, partitions
    return report, None


def synthetic_cases():
    return {
        "small_64": box(0, 0, 8, 8),
        "small_120": box(0, 0, 10, 12),
        "narrow_225": box(0, 0, 5, 45),
        "house_150": box(0, 0, 10, 15),
        "regular_400": box(0, 0, 20, 20),
        "rectangular_450": box(0, 0, 15, 30),
        "large_1200": box(0, 0, 30, 40),
        "L_shape": Polygon([(0, 0), (25, 0), (25, 10), (10, 10), (10, 25), (0, 25)]),
    }


def make_spec(name, partitions, scenario, epw):
    from epsm_core.pipeline import resolve_geometry
    from epsm_core.specs import (
        AirflowSpec,
        BuildingPart,
        BuildingSpec,
        ConstructionSpec,
        EnvelopeSpec,
        FacadeSpec,
        Footprint,
        GeometrySpec,
        GlazingSpec,
        LayerSpec,
        MaterialSpec,
        RoofSpec,
        ScheduleSpec,
        UseSpec,
        ZoneUseSpec,
        ZoningSpec,
    )
    from epsm_core.weather import climate_from_epw

    concrete = MaterialSpec(
        name="concrete", conductivity_w_mk=1.7, density_kg_m3=2300, specific_heat_j_kgk=880
    )
    insulation = MaterialSpec(
        name="insulation", conductivity_w_mk=0.04, density_kg_m3=40, specific_heat_j_kgk=840
    )
    slab = ConstructionSpec(layers=(LayerSpec(material=concrete, thickness_m=0.2),))
    insulated = ConstructionSpec(
        layers=(LayerSpec(material=insulation, thickness_m=0.15), *slab.layers)
    )
    parts = []
    for i, region in enumerate(partitions):
        p = region.polygon
        identifier = "service_core" if region.kind == "core" else f"habitable_{i}"
        parts.append(
            BuildingPart(
                identifier=identifier,
                footprint=Footprint(
                    exterior=tuple(p.exterior.coords)[:-1],
                    holes=tuple(tuple(r.coords)[:-1] for r in p.interiors),
                ),
                storey_elevations_m=(0, 3),
                conditioned=not (region.kind == "core" and scenario == "unconditioned"),
            )
        )
    geometry = GeometrySpec(
        parts=tuple(parts),
        zoning=ZoningSpec(mode="per_storey"),
        roof=RoofSpec(shape="flat", topology="compact"),
    )
    use = UseSpec(
        heating_setpoint=ScheduleSpec.constant(21),
        occupant_density_per_m2=0.017,
        equipment_gain_w_m2=1.68,
        lighting_gain_w_m2=0,
        dhw_kwh_m2_year=20,
    )
    ventilation = AirflowSpec(
        topology="FTX",
        envelope_infiltration_ach=0.2,
        mechanical_vent_l_s_m2=0.35,
        sensible_recovery_effectiveness=0.7,
    )
    service_use = use.updated(
        heating_setpoint=ScheduleSpec.constant(18 if scenario == "18C" else 21),
        occupant_density_per_m2=0,
        equipment_gain_w_m2=0,
        lighting_gain_w_m2=0,
        dhw_kwh_m2_year=0,
    )
    service_airflow = AirflowSpec(
        topology="S", envelope_infiltration_ach=0, mechanical_vent_l_s_m2=0, natural_exchange_ach=0
    )
    spec = BuildingSpec(
        identifier=f"{name}_{scenario}",
        geometry=geometry,
        facade=FacadeSpec(
            default_wwr=0.2, glazing=GlazingSpec(u_value_w_m2k=1.8, solar_heat_gain_coefficient=0.6)
        ),
        envelope=EnvelopeSpec(
            wall=insulated,
            top=insulated,
            ground=insulated,
            intermediate_floor=slab,
            weather_shell=slab,
            basement_wall=insulated,
            basement_ceiling=insulated,
        ),
        airflow=ventilation,
        use=use,
        climate=climate_from_epw(epw),
        # Native area bookkeeping includes heated service zones. Exact overrides
        # keep the occupied gains/ventilation identical while service gains are zero.
        load_reference_area_m2=geometry.conditioned_floor_area_m2,
    )
    # Fixed habitable gains, ventilation and DHW across all three scenarios.
    graph = resolve_geometry(spec).graph
    assignments = []
    for zone, data in graph.zones.items():
        if "service_core" in data["part_ids"]:
            if data["conditioned"]:
                assignments.append(
                    ZoneUseSpec(
                        zone_name=zone,
                        use=service_use,
                        airflow=service_airflow,
                        reference_area_m2=data["floor_area_m2"],
                        source="Experiment: no core gains, DHW or direct outdoor-air exchange",
                    )
                )
        else:
            assignments.append(
                ZoneUseSpec(
                    zone_name=zone,
                    use=use,
                    airflow=ventilation,
                    reference_area_m2=data["floor_area_m2"],
                    source="Experiment: identical occupied loads and outdoor air in every scenario",
                )
            )
    return spec.updated(zone_uses=tuple(assignments))


def experiment_compiler(energyplus):
    from epsm_core.compiler import EnergyPlusCompiler
    from epsm_core.surface_checks import validate_surfaces

    class SolidCoreCompiler(EnergyPlusCompiler):
        """An explicitly audited experiment extension; never edits Core package files."""

        def __init__(self):
            super().__init__(energyplus)
            self.compiler_hash = hashlib.sha256(
                (
                    self.compiler_hash + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
                ).encode()
            ).hexdigest()

        def compile(self, spec):
            model, report = super().compile(spec)
            data = report.data
            core_zones = {
                z for z, item in data["zones"].items() if "service_core" in item["part_ids"]
            }
            surfaces = model["BuildingSurface:Detailed"]
            changed = []
            for name, surface in surfaces.items():
                if (
                    surface["surface_type"] != "Wall"
                    or surface["outside_boundary_condition"] != "Surface"
                ):
                    continue
                other = surfaces[surface["outside_boundary_condition_object"]]
                if (surface["zone_name"] in core_zones) != (other["zone_name"] in core_zones):
                    surface["construction_name"] = "experiment_service_partition"
                    changed.append(name)
                    data["surfaces"][name]["construction_name"] = "experiment_service_partition"
            model.setdefault("Material", {})["experiment_partition_concrete"] = {
                "roughness": "MediumRough",
                "thickness": 0.15,
                "conductivity": 1.7,
                "density": 2300,
                "specific_heat": 880,
                "thermal_absorptance": 0.9,
                "solar_absorptance": 0.7,
                "visible_absorptance": 0.7,
            }
            model.setdefault("Construction", {})["experiment_service_partition"] = {
                "outside_layer": "experiment_partition_concrete"
            }
            # Remove assemblies no longer referenced, preserving foundation materials.
            used = {
                s["construction_name"]
                for kind in (
                    "BuildingSurface:Detailed",
                    "FenestrationSurface:Detailed",
                    "InternalMass",
                )
                for s in model.get(kind, {}).values()
            }
            used.update(
                s["footing_wall_construction_name"]
                for s in model.get("Foundation:Kiva", {}).values()
                if "footing_wall_construction_name" in s
            )
            for kind in ("Construction", "Construction:AirBoundary"):
                if kind in model:
                    model[kind] = {n: s for n, s in model[kind].items() if n in used}
                    if not model[kind]:
                        del model[kind]
            used_materials = {v for s in model.get("Construction", {}).values() for v in s.values()}
            used_materials.update(
                s["footing_material_name"]
                for s in model.get("Foundation:Kiva", {}).values()
                if "footing_material_name" in s
            )
            model["Material"] = {n: s for n, s in model["Material"].items() if n in used_materials}
            errors = list(self.validator.iter_errors(model))
            if errors:
                raise ValueError(str(errors[0]))
            validate_surfaces(model, data["zones"])
            assert changed and len(changed) % 2 == 0
            data["experiment"] = {
                "solid_core_partition": "150 mm concrete, k=1.7 W/mK, no door/infiltration/mixing model",
                "changed_surfaces": changed,
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            }
            run_id = hashlib.sha256(
                json.dumps(
                    [self._input_identity(spec), model], sort_keys=True, allow_nan=False
                ).encode()
            ).hexdigest()
            return model, report.updated(
                run_id=run_id, report_json=json.dumps(data, sort_keys=True)
            )

    return SolidCoreCompiler()


def run_case(name, partitions, scenario, epw, energyplus, output):
    import numpy as np
    from epsm_core.runner import EnergyPlusRunner

    spec = make_spec(name, partitions, scenario, epw)
    runner = EnergyPlusRunner(energyplus)
    runner.compiler = experiment_compiler(energyplus)
    result = runner.run(spec, output / name / scenario, timeout_seconds=180)
    compilation = json.loads((output / name / scenario / "compilation.json").read_text())
    core_zone = next(
        z
        for z, data in json.loads(compilation["report_json"])["zones"].items()
        if "service_core" in data["part_ids"]
    )
    temperatures = result.hourly[f"Zone Mean Air Temperature::{core_zone.upper()}"]
    return {
        "case": name,
        "scenario": scenario,
        "accepted": result.diagnostics["accepted"],
        "warnings": result.diagnostics["warning_count"],
        "heating_kwh": result.scalars["space_heating_demand_kwh"],
        "dhw_kwh": result.scalars["dhw_load_kwh"],
        "conditioned_area_m2": spec.geometry.conditioned_floor_area_m2,
        "fixed_occupied_area_m2": sum(p.polygon.area for p in partitions if p.kind != "core"),
        "core_temperature_min_c": float(np.min(temperatures)),
        "core_temperature_mean_c": float(np.mean(temperatures)),
        "core_temperature_p05_c": float(np.quantile(temperatures, 0.05)),
        "core_hours_above10_percent": float(np.mean(temperatures > 10) * 100),
        "artifact": str(output / name / scenario),
    }


def plot_layouts(cases, reports, partitions_by_case, output):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Polygon as PatchPolygon

    fig, axes = plt.subplots(2, 4, figsize=(14, 7), constrained_layout=True)
    for axis, (name, polygon) in zip(axes.flat, cases.items()):
        report = reports[name]
        regions = partitions_by_case.get(name)
        if regions:
            for region in regions:
                axis.add_patch(
                    PatchPolygon(
                        region.polygon.exterior.coords,
                        facecolor="#efb269" if region.kind == "core" else "#90b8da",
                        edgecolor="white",
                        linewidth=1,
                    )
                )
        else:
            axis.add_patch(
                PatchPolygon(polygon.exterior.coords, facecolor="#ddd", edgecolor="#666")
            )
        minx, miny, maxx, maxy = polygon.bounds
        axis.set_xlim(minx - 2, maxx + 2)
        axis.set_ylim(miny - 2, maxy + 2)
        axis.set_aspect("equal")
        axis.set_xlabel("metres")
        axis.set_ylabel("metres")
        detail = (
            f"depth {report['perimeter_depth_m']:.2f} m" if report["accepted"] else report["reason"]
        )
        axis.set_title(f"{name}: {polygon.area:g} m²\n{detail}", fontsize=10)
    fig.suptitle(
        "10% service-core scenario: blue habitable space, orange service core; grey layouts skipped"
    )
    fig.savefig(output / "layouts.png", dpi=180)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample", type=Path)
    parser.add_argument("--epw", required=True)
    parser.add_argument("--energyplus", required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if (args.output / "experiment-summary.json").exists():
        raise FileExistsError("Use a new output directory to preserve experiment evidence")
    policy = CorePolicy()
    cases = synthetic_cases()
    reports, partitions = {}, {}
    for name, polygon in cases.items():
        reports[name], plan = choose_core(polygon, policy)
        if plan:
            partitions[name] = plan
    sample_results = []
    if args.sample:
        sample = json.loads(args.sample.read_text())
        for row in sample["buildings"]:
            result, _ = choose_core(from_wkt(row["geometry_wkt"]), policy)
            sample_results.append({**row, **result})
    summary = {
        "policy": asdict(policy),
        "synthetic": reports,
        "sample": sample_results,
        "sample_reasons": dict(Counter(r["reason"] for r in sample_results)),
    }
    plot_layouts(cases, reports, partitions, args.output)
    jobs = [
        (name, scenario)
        for name in ("regular_400", "rectangular_450", "large_1200")
        for scenario in ("21C", "18C", "unconditioned")
        if name in partitions
    ]

    def run(job):
        name, scenario = job
        try:
            result = run_case(
                name, partitions[name], scenario, args.epw, args.energyplus, args.output
            )
            print(json.dumps(result), flush=True)
            return result
        except Exception as error:  # noqa: BLE001 - every failed experiment retains its diagnostics
            print(f"FAILED {name}/{scenario}: {type(error).__name__}: {error}", flush=True)
            return {"case": name, "scenario": scenario, "accepted": False, "error": str(error)}

    with ThreadPoolExecutor(max_workers=2) as executor:
        summary["simulations"] = list(executor.map(run, jobs))
    (args.output / "experiment-summary.json").write_text(json.dumps(summary, indent=2))
    print("Geometry:", json.dumps({k: v["reason"] for k, v in reports.items()}), flush=True)
    print("Stored-footprint sample:", summary["sample_reasons"], flush=True)


if __name__ == "__main__":
    main()
