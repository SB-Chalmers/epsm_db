"""Read-only screening of reported certificate area against building geometry."""

from collections import Counter, defaultdict
from datetime import date
import math
import numpy as np
import psycopg
from .epc import strict_value


def unique_number(observations, field, *, integer=False, verified=False):
    rows = [r for r in observations if r["source_field"] == field]
    if len(rows) != 1:
        return None
    row = rows[0]
    if verified:
        if (
            row["mapping_status"] != "verified"
            or row["value_status"] != "available"
            or row["owner_kind"] != "certificate"
        ):
            return None
        return row["normalized_value"]["value"] if row["normalized_value"] else None
    if len(row["raw_values"]) != 1:
        return None
    try:
        return strict_value(row["raw_values"][0], "integer" if integer else "number")
    except (ValueError, TypeError):
        return None


def ratios(footprint, storeys, atemp, basement=None, height=None):
    if any(v is None or not math.isfinite(v) or v <= 0 for v in (footprint, storeys, atemp)):
        return None
    gfa = footprint * storeys
    return {
        "footprint_m2": footprint,
        "above_ground_storeys": storeys,
        "atemp_m2": atemp,
        "estimated_above_ground_gfa_m2": gfa,
        "gfa_to_atemp": gfa / atemp,
        "atemp_to_gfa": atemp / gfa,
        "heated_basement_storeys": basement,
        "basement_group": "unknown"
        if basement is None
        else "zero"
        if basement == 0
        else "positive",
        "full_footprint_basement_sensitivity_gfa_to_atemp": None
        if basement is None
        else footprint * (storeys + basement) / atemp,
        "height_per_above_ground_storey_m": None if height is None else height / storeys,
    }


def distribution(values):
    values = [v for v in values if v is not None and math.isfinite(v)]
    return {
        "count": len(values),
        **(
            dict(
                zip(
                    ("p5", "p25", "median", "p75", "p95"),
                    map(float, np.percentile(values, [5, 25, 50, 75, 95])),
                )
            )
            if values
            else {}
        ),
    }


def summarize(records):
    metrics = (
        "gfa_to_atemp",
        "atemp_to_gfa",
        "height_per_above_ground_storey_m",
        "full_footprint_basement_sensitivity_gfa_to_atemp",
    )
    result = {metric: distribution([r[metric] for r in records]) for metric in metrics}
    result["gfa_atemp_correlation"] = (
        float(
            np.corrcoef(
                [r["estimated_above_ground_gfa_m2"] for r in records],
                [r["atemp_m2"] for r in records],
            )[0, 1]
        )
        if len(records) > 1
        and len({r["atemp_m2"] for r in records}) > 1
        and len({r["estimated_above_ground_gfa_m2"] for r in records}) > 1
        else None
    )
    result["gfa_atemp_screening"] = dict(
        Counter(
            "severe"
            if r["gfa_to_atemp"] < 0.5 or r["gfa_to_atemp"] > 2
            else "within_0.8_to_1.5"
            if 0.8 <= r["gfa_to_atemp"] <= 1.5
            else "outside_band"
            for r in records
        )
    )
    result["height_screening"] = dict(
        Counter(
            "within_2_to_5_m" if 2 <= r["height_per_above_ground_storey_m"] <= 5 else "outside_band"
            for r in records
            if r["height_per_above_ground_storey_m"] is not None
        )
    )
    return result


def assess(url, dataset="uddevalla"):
    def read(c, query):
        cur = c.execute(query, (dataset,))
        return [dict(zip([d.name for d in cur.description], r)) for r in cur]

    with psycopg.connect(url, options="-c default_transaction_read_only=on") as c:
        buildings = read(
            c,
            "SELECT building_id,selection_role,status_verified,is_existing,geometry_status,ST_IsValid(geom) AS valid_geom,area_m2,component_count,purposes FROM mart.building_facts WHERE dataset_id=%s",
        )
        certs = {
            r["certificate_id"]: r
            for r in read(c, "SELECT * FROM mart.epc_certificates WHERE dataset_id=%s")
        }
        relations = read(c, "SELECT * FROM mart.building_epc WHERE dataset_id=%s")
        observations = read(
            c,
            "SELECT certificate_id,source_field,raw_values,owner_kind,normalized_value,value_status,mapping_status FROM mart.epc_observations WHERE dataset_id=%s",
        )
        subjects = read(
            c,
            "SELECT certificate_id,temporal_status,outcome FROM mart.epc_subject_results WHERE dataset_id=%s",
        )
        heights = read(
            c,
            "SELECT h.*,g.geometry_hash FROM mart.component_heights h JOIN mart.building_components g USING(dataset_id,component_id) WHERE h.dataset_id=%s",
        )
    obs, links, temporal, hs = (defaultdict(list) for _ in range(4))
    for r in observations:
        obs[r["certificate_id"]].append(r)
    for r in relations:
        links[r["building_id"]].append(r["certificate_id"])
    for r in subjects:
        temporal[r["certificate_id"]].append(r)
    for r in heights:
        hs[r["building_id"]].append(r)
    records, older, warnings = [], [], []
    exclusions = Counter()
    for building in buildings:
        if (
            building["selection_role"] != "primary"
            or not building["status_verified"]
            or not building["is_existing"]
        ):
            exclusions["not_primary_verified_existing"] += 1
            continue
        if (
            building["geometry_status"] != "resolved"
            or not building["valid_geom"]
            or not building["area_m2"]
            or building["area_m2"] <= 0
        ):
            exclusions["invalid_or_ambiguous_geometry"] += 1
            continue
        eligible = []
        candidate_dates = []
        for cid in sorted(set(links[building["building_id"]])):
            cert = certs[cid]
            if (
                cert["subject_count"] != 1
                or cert["loaded_building_count"] != 1
                or cert["matching_status"] != "fully_matched"
            ):
                exclusions["shared_or_incomplete_certificate_relation"] += 1
                continue
            if cert["date_status"] != "verified" or cert["certificate_date"] is None:
                exclusions["unverified_certificate_date"] += 1
                continue
            candidate_dates.append(cert["certificate_date"].isoformat())
            atemp = unique_number(obs[cid], "EgenAtemp", verified=True)
            storeys = unique_number(obs[cid], "EgenAntalPlan", integer=True)
            basement = unique_number(obs[cid], "EgenAntalKallarplan", integer=True)
            if basement is not None and basement < 0:
                basement = None
            value = ratios(building["area_m2"], storeys, atemp, basement)
            if value is None:
                exclusions["missing_conflicting_or_nonpositive_area_or_storeys"] += 1
                continue
            height_rows = hs[building["building_id"]]
            height, basis, roof = None, "missing_or_unresolved", None
            if building["component_count"] == 1 and len(height_rows) == 1:
                h = height_rows[0]
                roof = h["roof_form"]
                if (
                    h["height_status"] == "determined"
                    and h["input_geometry_hash"] == h["geometry_hash"]
                    and h["details"].get("evidence_resolution") == "resolved"
                ):
                    if (
                        h["eave_height_m"] is not None
                        and math.isfinite(h["eave_height_m"])
                        and h["eave_height_m"] > 0
                    ):
                        height, basis = h["eave_height_m"], "eave"
                    elif (
                        roof == "flat"
                        and h["representative_height_m"] is not None
                        and math.isfinite(h["representative_height_m"])
                        and h["representative_height_m"] > 0
                    ):
                        height, basis = h["representative_height_m"], "flat_roof_median"
                    else:
                        basis = "pitched_or_unclear_without_eave"
            elif building["component_count"] > 1:
                basis = "multiple_components"
            value["height_per_above_ground_storey_m"] = None if height is None else height / storeys
            approved = cert["certificate_date"]
            value.update(
                building_id=str(building["building_id"]),
                certificate_id=cid,
                approval_date=approved.isoformat(),
                regime=cert["regime"],
                purposes=building["purposes"],
                height_basis=basis,
                roof_form=roof,
                certificate_age_years=(date(2026, 10, 12) - approved).days / 365.25,
            )
            value["warning"] = any(r["temporal_status"] != "unknown" for r in temporal[cid])
            eligible.append(value)
        if not eligible:
            exclusions["no_eligible_certificate_for_building"] += 1
            continue
        latest = max(candidate_dates)
        chosen = [r for r in eligible if r["approval_date"] == latest]
        if candidate_dates.count(latest) != 1:
            exclusions["tied_latest_approval_date"] += 1
            continue
        if not chosen:
            exclusions["latest_certificate_has_invalid_quantities"] += 1
            continue
        for r in eligible:
            if r["approval_date"] != latest:
                older.append(r)
        chosen = chosen[0]
        (warnings if chosen["warning"] else records).append(chosen)
    groups = defaultdict(list)
    for r in records:
        for field in ("regime", "roof_form", "basement_group", "height_basis"):
            groups[f"{field}:{r[field]}"].append(r)
        for purpose in r["purposes"]:
            groups[f"purpose:{purpose}"].append(r)
        groups["age:" + ("0_to_10" if r["certificate_age_years"] <= 10 else "over_10")].append(r)
    return {
        "dataset": dataset,
        "building_count": len(buildings),
        "main": summarize(records),
        "older_certificates": summarize(older),
        "temporal_warnings": summarize(warnings),
        "exclusions": dict(exclusions),
        "groups": {k: summarize(v) for k, v in groups.items()},
        "interpretation": "Diagnostic bands, not regulatory limits. GFA is union footprint times above-ground storeys; basement metric assumes a full footprint only as sensitivity. Atemp excludes warm garages. Heights use eaves or flat-roof median points relative to ground; shared areas are never allocated.",
        "main_records": records,
        "warning_records": warnings,
        "older_records": older,
    }
