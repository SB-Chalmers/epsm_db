"""Compare recorded EPC outcomes with the exact-house rule on the SAME loaded stock.

Run after dataset load/report-epc. This script never changes database records.
"""

from __future__ import annotations
import argparse
from collections import Counter
import json
import os
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator
import shapely

from sweden_ubem.database import _connect, validate_dataset
from sweden_ubem.linkage import derive_epc_candidates, property_match_key

SITES = [
    "HERRESTAD 12:4",
    "NYPONBUSKEN 16",
    "BIKUPAN 24",
    "RYDINGSBERG 1",
    "SKÄLLÄCKERÖD 1:40",
    "LINDSTAM 2",
]


def evaluate(url, dataset, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    with _connect(url) as c:

        def rows(query):
            cur = c.execute(query, (dataset,))
            return [dict(zip([d.name for d in cur.description], row)) for row in cur]

        facts = rows(
            "SELECT building_id,house_number,linkage_eligible,area_m2,component_count FROM mart.building_facts WHERE dataset_id=%s"
        )
        props = rows(
            "SELECT building_id,property_unit_id,overlap_fraction,is_primary AS is_primary_property FROM mart.building_properties WHERE dataset_id=%s"
        )
        labels = rows(
            "SELECT property_unit_id,municipality_code,normalized_label AS designation_normalized FROM mart.property_designations WHERE dataset_id=%s"
        )
        subjects = rows(
            "SELECT s.subject_id AS epc_subject_id,s.certificate_id,c.formular_id,s.municipality_code,s.designation_normalized,s.house_number,s.original_values FROM mart.epc_subjects s JOIN mart.epc_certificates c USING(dataset_id,certificate_id) WHERE dataset_id=%s"
        )
        results = rows("SELECT * FROM mart.epc_subject_results WHERE dataset_id=%s")
        candidates = rows("SELECT * FROM mart.epc_candidates WHERE dataset_id=%s")
        certificates = rows("SELECT * FROM mart.epc_certificates WHERE dataset_id=%s")
        counts = {
            name: c.execute(
                f"SELECT count(*) FROM mart.{name} WHERE dataset_id=%s", (dataset,)
            ).fetchone()[0]
            for name in (
                "building_facts",
                "building_components",
                "component_heights",
                "building_addresses",
                "epc_subjects",
                "epc_certificates",
                "epc_subject_results",
                "epc_site_events",
            )
        }
        relation_count = c.execute(
            "SELECT count(*) FROM mart.building_epc WHERE dataset_id=%s", (dataset,)
        ).fetchone()[0]
        stock_size = c.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        events = rows("SELECT * FROM mart.epc_site_events WHERE dataset_id=%s")
        source_versions = rows(
            "SELECT layer,policy_version,code_version,input_versions FROM mart.process_runs WHERE dataset_id=%s AND status='complete'"
        )
        for b in facts:
            b["building_id"] = str(b["building_id"])
        for p in props:
            p["building_id"] = str(p["building_id"])
        for s in subjects:
            s["property_match_key"] = property_match_key(
                s["municipality_code"], s["designation_normalized"]
            )
        baseline = derive_epc_candidates(facts, props, labels, subjects)
        base_matched = {r["epc_subject_id"] for r in baseline if r["decision"] == "accepted"}
        final_matched = {r["subject_id"] for r in results if r["outcome"] == "matched"}
        by_subject = {r["subject_id"]: r for r in results}
        comparison = dict(
            dataset=dataset,
            validation=validate_dataset(url, dataset),
            counts=counts,
            database_bytes=stock_size,
            baseline_scope="exact-property-house rule on identical rebuilt geometry/register/property/EPC inputs",
            baseline_accepted_relations=sum(r["decision"] == "accepted" for r in baseline),
            final_accepted_relations=relation_count,
            baseline_matched_subjects=len(base_matched),
            final_matched_subjects=len(final_matched),
            recovered_subjects=sorted(final_matched - base_matched),
            withheld_subjects=sorted(base_matched - final_matched),
            recovered=[by_subject[s] for s in sorted(final_matched - base_matched)],
            withheld=[by_subject[s] for s in sorted(base_matched - final_matched)],
            certificates=dict(Counter(c["matching_status"] for c in certificates)),
            accepted_methods=dict(
                Counter(
                    r["details"]["match_method"] for r in candidates if r["decision"] == "accepted"
                )
            ),
            source_versions=source_versions,
            site_events=events,
        )
        school = [s for s in subjects if str(s["original_values"].get("EgenTypkod")) == "825"]
        comparison["school_property_subjects"] = dict(
            total=len(school),
            matched=sum(s["epc_subject_id"] in final_matched for s in school),
            unmatched=[
                by_subject[s["epc_subject_id"]]
                for s in school
                if s["epc_subject_id"] not in final_matched
            ],
        )
        comparison["school_cases"] = {
            site: [r for r in results if r["designation_normalized"] == site] for site in SITES
        }
        (output / "evaluation.json").write_text(
            json.dumps(comparison, indent=2, ensure_ascii=False, default=str)
        )
        plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
        fig, axes = plt.subplots(1, 2, figsize=(10, 4))
        totals = len(subjects)
        axes[0].bar(
            ["Exact-house baseline", "Revised rules"],
            [totals - len(base_matched), totals - len(final_matched)],
            color=["#7d8997", "#177e89"],
        )
        for i, v in enumerate([totals - len(base_matched), totals - len(final_matched)]):
            axes[0].text(i, v + 1, str(v), ha="center")
        axes[0].set_title("Subjects without an accepted identity link")
        axes[0].set_ylim(0, max(totals - len(base_matched), totals - len(final_matched)) * 1.18)
        axes[1].bar(
            ["Recovered", "Withheld"],
            [len(final_matched - base_matched), len(base_matched - final_matched)],
            color=["#177e89", "#bc4737"],
        )
        for i, v in enumerate(
            [len(final_matched - base_matched), len(base_matched - final_matched)]
        ):
            axes[1].text(i, v + 0.2, str(v), ha="center")
        axes[1].set_title("Changes on identical source inputs")
        fig.tight_layout()
        fig.savefig(output / "coverage.png", dpi=170)
        plt.close(fig)
        reasons = Counter(
            code for r in results if r["outcome"] != "matched" for code in r["reasons"]
        )
        items = reasons.most_common(10)
        fig, ax = plt.subplots(figsize=(10, 4.6))
        ax.barh(
            [k.replace("_", " ") for k, v in reversed(items)],
            [v for k, v in reversed(items)],
            color="#2355a3",
        )
        ax.set_xlabel("Unmatched subjects carrying the reason (categories overlap)")
        fig.tight_layout()
        fig.savefig(output / "reasons.png", dpi=170)
        plt.close(fig)
        maps = []
        for i, site in enumerate(SITES, 1):
            ss = [r for r in results if r["designation_normalized"] == site]
            sids = [r["subject_id"] for r in ss]
            subject_candidates = [r for r in candidates if r["subject_id"] in sids]
            units = sorted(
                {p["property_unit_id"] for p in labels if p["designation_normalized"] == site}
            )
            if not units:
                continue
            cur = c.execute(
                "SELECT ST_AsBinary(ST_Union(geom)) FROM mart.property_parts WHERE dataset_id=%s AND property_unit_id=ANY(%s)",
                (dataset, units),
            )
            property_geom = shapely.from_wkb(bytes(cur.fetchone()[0]))
            bids = sorted({str(r["building_id"]) for r in subject_candidates})
            if not bids:
                continue
            cur = c.execute(
                "SELECT building_id,house_number,ST_AsBinary(geom) FROM mart.building_facts WHERE dataset_id=%s AND building_id=ANY(%s::uuid[])",
                (dataset, bids),
            )
            bb = [(str(b), h, shapely.from_wkb(bytes(w))) for b, h, w in cur]
            accepted = {
                str(r["building_id"]) for r in subject_candidates if r["decision"] == "accepted"
            }
            focus_houses = {
                r["house_number"]
                for r in ss
                if r["outcome"] != "matched" or r["subject_id"] in final_matched - base_matched
            }
            fig, ax = plt.subplots(figsize=(8, 5.2))
            gpd.GeoSeries([property_geom], crs=3006).boundary.plot(
                ax=ax, color="#177e89", linewidth=2
            )
            for bid, h, geom in bb:
                gpd.GeoSeries([geom], crs=3006).plot(
                    ax=ax,
                    color="#409365" if bid in accepted else "#d6dce4",
                    edgecolor="#2355a3",
                    alpha=0.8,
                )
                point = geom.representative_point()
                if bid in accepted or h in focus_houses or geom.area >= 300:
                    ax.text(point.x, point.y, str(h), fontsize=8, ha="center")
            extent = shapely.union_all([geometry for _, _, geometry in bb]).bounds
            padding = max(20, 0.12 * max(extent[2] - extent[0], extent[3] - extent[1]))
            ax.set_xlim(extent[0] - padding, extent[2] + padding)
            ax.set_ylim(extent[1] - padding, extent[3] + padding)
            ax.xaxis.set_major_locator(MaxNLocator(4, integer=True))
            ax.yaxis.set_major_locator(MaxNLocator(5, integer=True))
            ax.tick_params(labelsize=8)
            ax.set_aspect("equal")
            ax.ticklabel_format(useOffset=False, style="plain")
            ax.set_title(f"{site}: current candidate footprints")
            ax.set_xlabel("SWEREF 99 TM easting (m)")
            ax.set_ylabel("northing (m)")
            fig.tight_layout()
            filename = f"site-{i}.png"
            fig.savefig(output / filename, dpi=170)
            plt.close(fig)
            maps.append(
                dict(property=site, file=filename, subjects=ss, accepted_buildings=sorted(accepted))
            )
        (output / "maps.json").write_text(
            json.dumps(maps, indent=2, ensure_ascii=False, default=str)
        )
    return {
        k: comparison[k]
        for k in (
            "baseline_matched_subjects",
            "final_matched_subjects",
            "final_accepted_relations",
            "certificates",
            "accepted_methods",
        )
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="uddevalla")
    parser.add_argument("--output", type=Path, required=True)
    a = parser.parse_args()
    print(json.dumps(evaluate(os.environ["UBEM_DATABASE_URL"], a.dataset, a.output), indent=2))
