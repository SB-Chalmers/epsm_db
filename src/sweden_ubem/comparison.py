"""Read-only comparison with legacy EPC outputs, never a relation loader."""

from collections import defaultdict
from pathlib import Path
import json

from .database import _connect
from .dataset import selected_parquet
from .hashing import sha256_file
from .linkage import normalize_designation


def relation_key(row):
    return (
        str(row["building_id"]),
        int(row["formular_id"]),
        str(row["municipality_code"]),
        normalize_designation(row["designation"]),
        None if row["house_number"] is None else int(row["house_number"]),
    )


def compare_relations(current, oracle, buildings):
    groups = [defaultdict(list), defaultdict(list)]
    for group, rows in zip(groups, (current, oracle)):
        for row in rows:
            group[relation_key(row)].append(row)
    differences = []
    for key in sorted(groups[0].keys() | groups[1].keys(), key=str):
        left, right = groups[0].get(key, []), groups[1].get(key, [])
        if len(left) == len(right) == 1 and left[0]["decision"] == right[0]["decision"]:
            continue
        building = buildings.get(key[0], {})
        reason = "policy_or_source_difference"
        if len(left) > 1 or len(right) > 1:
            reason = "comparison_key_not_unique"
        elif not left and building.get("resolution_status") != "resolved":
            reason = "current_geometry_unresolved"
        elif not left and building.get("municipality_code") is None:
            reason = "municipality_conflict"
        differences.append(
            {
                "building_id": key[0],
                "formular_id": key[1],
                "municipality_code": key[2],
                "designation": key[3],
                "house_number": key[4],
                "current": left,
                "oracle": right,
                "reason": reason,
                "diagnostic_status": "unclassified"
                if reason == "policy_or_source_difference"
                else "classified",
            }
        )
    return differences


def compare_oracle(url, dataset, oracle_root, output):
    output = Path(output)
    if output.exists():
        raise FileExistsError("Choose a new comparison report path")
    with _connect(url) as c:
        cur = c.execute(
            "SELECT building_id,municipality_code,geometry_status FROM mart.building_facts WHERE dataset_id=%s",
            (dataset,),
        )
        buildings = {str(b): dict(municipality_code=m, resolution_status=r) for b, m, r in cur}
        cur = c.execute(
            """SELECT r.building_id,cert.formular_id,s.municipality_code,
                          s.designation_normalized AS designation,s.house_number,r.decision
                          FROM mart.epc_candidates r JOIN mart.epc_subjects s
                          USING(dataset_id,subject_id) JOIN mart.epc_certificates cert
                          ON cert.dataset_id=s.dataset_id AND cert.certificate_id=s.certificate_id
                          WHERE r.dataset_id=%s""",
            (dataset,),
        )
        # Formular ID is on the certificate, not duplicated on the subject.
        current = [dict(zip([d.name for d in cur.description], row)) for row in cur]
    files = sorted((Path(oracle_root) / "candidate_links").glob("kommunkod=*/part.parquet"))
    if not files:
        raise FileNotFoundError("No oracle candidate partitions")
    oracle, hashes = [], {}
    for path in files:
        rows = list(selected_parquet(path, "building_id", buildings))
        if not rows:
            continue
        before = sha256_file(path)
        oracle.extend(
            dict(
                building_id=r["building_id"],
                formular_id=r["formular_id"],
                municipality_code=r["epc_municipality_code"],
                designation=r["epc_property_normalized"],
                house_number=r["epc_house_number"],
                decision=r["decision"],
                source_reason=r.get("rejection_reason"),
            )
            for r in rows
        )
        if sha256_file(path) != before:
            raise ValueError("Oracle source changed during comparison")
        hashes[str(path.resolve())] = before
    differences = compare_relations(current, oracle, buildings)
    report = dict(
        dataset=dataset,
        comparison_scope="loaded_buildings",
        oracle_files=hashes,
        current_relations=len(current),
        oracle_relations=len(oracle),
        differences=differences,
        unclassified_differences=sum(d["diagnostic_status"] == "unclassified" for d in differences),
        warnings=["Legacy outputs are comparison evidence, not imported authoritative links."],
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, default=str)
    return dict(
        report_path=str(output),
        differences=len(differences),
        unclassified_differences=report["unclassified_differences"],
    )
