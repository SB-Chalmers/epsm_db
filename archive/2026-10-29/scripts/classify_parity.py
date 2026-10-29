"""Classify verified Atemp changes and equivalent derived geometry precisely."""

import gzip
import json
from pathlib import Path
from collections import defaultdict
import psycopg
from sweden_ubem.epc import strict_value


def equivalent_derived_geometry(output, url):
    pairs = defaultdict(lambda: {"old": [], "new": []})
    with gzip.open(output / "building_facts-differences.jsonl.gz", "rt") as f:
        for line in f:
            r = json.loads(line)
            side = "old" if r["baseline_count"] > r["current_count"] else "new"
            pairs[r["row"]["building_id"]][side].append(
                (r["row"], abs(r["baseline_count"] - r["current_count"]))
            )
    accepted = 0
    with psycopg.connect(url, options="-c default_transaction_read_only=on") as c:
        for pair in pairs.values():
            if len(pair["old"]) != 1 or len(pair["new"]) != 1:
                continue
            (old, n), (new, m) = pair["old"][0], pair["new"][0]
            if n != 1 or m != 1 or not old.get("geom") or not new.get("geom"):
                continue
            if {k: v for k, v in old.items() if k not in ("geom", "area_m2")} != {
                k: v for k, v in new.items() if k not in ("geom", "area_m2")
            }:
                continue
            if (
                old.get("area_m2") is None
                or new.get("area_m2") is None
                or abs(old["area_m2"] - new["area_m2"]) > 1e-6
            ):
                continue
            equal = c.execute(
                "SELECT ST_Equals(ST_GeomFromEWKB(%s),ST_GeomFromEWKB(%s))",
                (bytes.fromhex(old["geom"]), bytes.fromhex(new["geom"])),
            ).fetchone()[0]
            if equal:
                accepted += 1
    return accepted


def classify(output, database_url=None):
    output = Path(output)
    report = json.loads((output / "report.json").read_text())
    result = {
        "equal_functional_values": report["equal_functional_values"],
        "verified_atemp_observations": 0,
        "equivalent_derived_geometry_buildings": 0,
        "derived_geometry_area_tolerance_m2": 1e-6,
        "unexplained_differences": {},
    }
    for view, counts in report["functional_views"].items():
        missing, added = counts["missing_rows"], counts["added_rows"]
        if (
            not missing
            and not added
            and not counts["added_columns"]
            and not counts["removed_columns"]
        ):
            continue
        if (
            view == "epc_observations"
            and not counts["added_columns"]
            and not counts["removed_columns"]
        ):
            pairs = defaultdict(lambda: {"old": [], "new": []})
            with gzip.open(output / f"{view}-differences.jsonl.gz", "rt") as f:
                for line in f:
                    r = json.loads(line)
                    key = (
                        r["row"]["certificate_id"],
                        r["row"]["source_field"],
                        r["row"]["observation_id"],
                    )
                    if r["baseline_count"] > r["current_count"]:
                        pairs[key]["old"].append(
                            (r["row"], r["baseline_count"] - r["current_count"])
                        )
                    else:
                        pairs[key]["new"].append(
                            (r["row"], r["current_count"] - r["baseline_count"])
                        )
            for (certificate, field, _), pair in pairs.items():
                if (
                    not certificate.startswith("efter_v260:")
                    or field != "EgenAtemp"
                    or len(pair["old"]) != 1
                    or len(pair["new"]) != 1
                ):
                    continue
                (old, n), (new, m) = pair["old"][0], pair["new"][0]
                if n != 1 or m != 1 or old["mapping_status"] != "unverified":
                    continue
                expected = {
                    **old,
                    "mapping_status": "verified",
                    "normalized_value": None,
                    "value_status": "conflicting" if len(old["raw_values"]) > 1 else "failed",
                }
                if len(old["raw_values"]) == 1:
                    try:
                        expected["normalized_value"] = {
                            "value": strict_value(old["raw_values"][0], "number")
                        }
                        expected["value_status"] = "available"
                    except (ValueError, TypeError):
                        pass
                if expected == new:
                    missing -= 1
                    added -= 1
                    result["verified_atemp_observations"] += 1
        if (
            view == "building_facts"
            and database_url
            and (missing or added)
            and not counts["added_columns"]
            and not counts["removed_columns"]
        ):
            accepted = equivalent_derived_geometry(output, database_url)
            missing -= accepted
            added -= accepted
            result["equivalent_derived_geometry_buildings"] = accepted
        if missing or added or counts["added_columns"] or counts["removed_columns"]:
            result["unexplained_differences"][view] = {
                **counts,
                "unexplained_missing_rows": missing,
                "unexplained_added_rows": added,
            }
    result["accepted"] = not result["unexplained_differences"]
    (output / "classification.json").write_text(json.dumps(result, indent=2))
    return result
