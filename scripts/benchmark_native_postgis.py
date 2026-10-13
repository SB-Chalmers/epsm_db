"""Compare native PG15 and the container on identical real-source samples.

Only disposable ubem_test_ databases are written. National source reads and seed
loading are outside the timings. Constraints, indexes, COPY code and sample data
are the same on both servers. This is not a national cold-cache runtime forecast.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from statistics import median
from time import perf_counter
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
import shapely

from sweden_ubem.hashing import content_hash
from sweden_ubem.jobs import bulk_insert


def sample(url, limit):
    with psycopg.connect(url, row_factory=dict_row) as c:
        c.execute("SET TRANSACTION READ ONLY")
        lineage = c.execute(
            "SELECT * FROM stock.component_source_feature TABLESAMPLE SYSTEM(3) "
            "REPEATABLE(42) LIMIT %s",
            (limit,),
        ).fetchall()
        parents = {}
        for start in range(0, len(lineage), 8192):
            ids = [r["component_id"] for r in lineage[start : start + 8192]]
            for r in c.execute(
                "SELECT dataset_id,component_id,building_id,geometry_hash,"
                "ST_AsBinary(geom) AS geom FROM stock.geometry_component "
                "WHERE dataset_id='national' AND component_id=ANY(%s)",
                (ids,),
            ):
                r["geom"] = bytes(r["geom"])
                parents[r["component_id"]] = r
    if not lineage or len(parents) == 0:
        raise ValueError("No real-source sample")
    return lineage, list(parents.values())


DDL = """
CREATE SCHEMA bench;
CREATE TABLE bench.building(dataset_id text,building_id uuid,
 PRIMARY KEY(dataset_id,building_id));
CREATE TABLE bench.source_version(source_version_id text PRIMARY KEY);
CREATE TABLE bench.component(dataset_id text,component_id text,building_id uuid NOT NULL,
 geometry_hash text NOT NULL,geom geometry(Geometry,3006) NOT NULL,
 PRIMARY KEY(dataset_id,component_id),
 FOREIGN KEY(dataset_id,building_id) REFERENCES bench.building,
 CHECK(component_id=building_id::text||':'||geometry_hash));
CREATE INDEX component_geom ON bench.component USING gist(geom);
CREATE INDEX component_building ON bench.component(dataset_id,building_id);
CREATE TABLE bench.lineage(dataset_id text,component_id text,source_fid bigint,
 source_version_id text,disposition text NOT NULL,original_values jsonb NOT NULL,
 PRIMARY KEY(dataset_id,component_id,source_fid),
 FOREIGN KEY(dataset_id,component_id) REFERENCES bench.component,
 FOREIGN KEY(source_version_id) REFERENCES bench.source_version);
CREATE TABLE bench.geometry_sink(LIKE bench.component INCLUDING ALL);
ALTER TABLE bench.geometry_sink ADD FOREIGN KEY(dataset_id,building_id)
 REFERENCES bench.building(dataset_id,building_id);
"""


def load(url, table, records, size=8192):
    with psycopg.connect(url) as c:
        for start in range(0, len(records), size):
            bulk_insert(c, table, records[start : start + size])


def run(args):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    lineage, components = sample(args.source_url, args.rows)
    buildings = list(
        {
            (r["dataset_id"], r["building_id"]): {
                "dataset_id": r["dataset_id"],
                "building_id": r["building_id"],
            }
            for r in components
        }.values()
    )
    versions = [{"source_version_id": v} for v in sorted({r["source_version_id"] for r in lineage})]
    name = "ubem_test_native_compare_" + uuid4().hex
    admins = {"native_pg15": args.native_admin_url, "container_pg17": args.container_admin_url}
    targets, created, environments, results = {}, [], {}, []
    geometries = {}
    try:
        for label, admin in admins.items():
            info = conninfo_to_dict(admin)
            info["dbname"] = name
            targets[label] = make_conninfo(**info)
            with psycopg.connect(admin, autocommit=True) as c:
                c.execute(
                    sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name))
                )
            created.append(label)
            with psycopg.connect(targets[label]) as c:
                c.execute("CREATE EXTENSION postgis")
                c.execute(DDL)
                environments[label] = {
                    "postgres": c.execute("SELECT version()").fetchone()[0],
                    "postgis": c.execute("SELECT postgis_full_version()").fetchone()[0],
                    "settings": dict(
                        c.execute(
                            "SELECT name,setting FROM pg_settings WHERE name=ANY(%s)",
                            (
                                [
                                    "shared_buffers",
                                    "work_mem",
                                    "maintenance_work_mem",
                                    "fsync",
                                    "synchronous_commit",
                                    "full_page_writes",
                                    "wal_sync_method",
                                    "max_wal_size",
                                ],
                            ),
                        ).fetchall()
                    ),
                }
            load(targets[label], "bench.building", buildings)
            load(targets[label], "bench.source_version", versions)
            load(targets[label], "bench.component", components)
            with psycopg.connect(targets[label], autocommit=True) as c:
                c.execute("VACUUM ANALYZE bench.component")
                c.execute("ANALYZE bench.building")
        for repeat in range(4):
            # First round is a warmup; alternate environment order thereafter.
            for label in list(targets) if repeat % 2 == 0 else list(reversed(targets)):
                url = targets[label]
                for size, reuse in [(1024, False), (8192, True)]:
                    with psycopg.connect(url) as c:
                        c.execute("TRUNCATE bench.lineage")
                    shared = psycopg.connect(url) if reuse else None
                    start = perf_counter()
                    try:
                        for offset in range(0, len(lineage), size):
                            c = shared or psycopg.connect(url)
                            try:
                                with c.transaction():
                                    bulk_insert(c, "bench.lineage", lineage[offset : offset + size])
                            finally:
                                if shared is None:
                                    c.close()
                    finally:
                        if shared:
                            shared.close()
                    elapsed = perf_counter() - start
                    with psycopg.connect(url) as c:
                        assert c.execute("SELECT count(*) FROM bench.lineage").fetchone()[0] == len(
                            lineage
                        )
                    result = dict(
                        server=label,
                        workload="lineage",
                        batch_rows=size,
                        reuse_connection=reuse,
                        repeat=repeat,
                        warmup=repeat == 0,
                        rows=len(lineage),
                        seconds=elapsed,
                    )
                    results.append(result)
                    print(json.dumps(result), flush=True)
                with psycopg.connect(url) as c:
                    c.execute("TRUNCATE bench.geometry_sink")
                start = perf_counter()
                with psycopg.connect(url) as c:
                    for offset in range(0, len(components), 8192):
                        with c.transaction():
                            bulk_insert(
                                c, "bench.geometry_sink", components[offset : offset + 8192]
                            )
                result = dict(
                    server=label,
                    workload="geometry_ingestion",
                    batch_rows=8192,
                    reuse_connection=True,
                    repeat=repeat,
                    warmup=repeat == 0,
                    rows=len(components),
                    seconds=perf_counter() - start,
                )
                with psycopg.connect(url) as c:
                    assert c.execute("SELECT count(*) FROM bench.geometry_sink").fetchone()[
                        0
                    ] == len(components)
                results.append(result)
                print(json.dumps(result), flush=True)
                with psycopg.connect(url) as c:
                    start = perf_counter()
                    value = c.execute(
                        "SELECT sum(ST_Area(ST_Intersection(geom,ST_Buffer(ST_PointOnSurface(geom),5)))) "
                        "FROM (SELECT geom FROM bench.component WHERE ST_IsValid(geom) "
                        "ORDER BY component_id LIMIT 32768) g"
                    ).fetchone()[0]
                    result = dict(
                        server=label,
                        workload="spatial_geometry",
                        repeat=repeat,
                        warmup=repeat == 0,
                        seconds=perf_counter() - start,
                        area_sum=value,
                    )
                    results.append(result)
                    print(json.dumps(result), flush=True)
                    if repeat == 0:
                        geometries[label] = dict(
                            c.execute(
                                "SELECT component_id,ST_AsBinary(ST_Normalize(ST_Intersection(geom,"
                                "ST_Buffer(ST_PointOnSurface(geom),5)))) FROM bench.component "
                                "WHERE ST_IsValid(geom) ORDER BY component_id LIMIT 2048"
                            ).fetchall()
                        )
        summaries = []
        for workload, batch in [
            ("lineage", 1024),
            ("lineage", 8192),
            ("geometry_ingestion", 8192),
            ("spatial_geometry", None),
        ]:
            times = {
                label: median(
                    r["seconds"]
                    for r in results
                    if not r["warmup"]
                    and r["server"] == label
                    and r["workload"] == workload
                    and r.get("batch_rows") == batch
                )
                for label in targets
            }
            summaries.append(
                dict(
                    workload=workload,
                    batch_rows=batch,
                    median_seconds=times,
                    native_speedup=times["container_pg17"] / times["native_pg15"],
                )
            )
        mismatch, max_distance, max_area_delta = 0, 0, 0
        assert geometries["native_pg15"].keys() == geometries["container_pg17"].keys()
        for key, native in geometries["native_pg15"].items():
            other = geometries["container_pg17"][key]
            if bytes(native) != bytes(other):
                mismatch += 1
                a, b = shapely.from_wkb(bytes(native)), shapely.from_wkb(bytes(other))
                max_distance = max(max_distance, shapely.hausdorff_distance(a, b))
                max_area_delta = max(max_area_delta, abs(a.area - b.area))
        report = dict(
            timestamp=datetime.now(timezone.utc).isoformat(),
            rows=len(lineage),
            parent_components=len(components),
            parent_buildings=len(buildings),
            sample_signature=content_hash(lineage),
            environments=environments,
            results=results,
            summaries=summaries,
            geometry_comparison=dict(
                rows=len(geometries["native_pg15"]),
                exact_wkb_differences=mismatch,
                maximum_hausdorff_distance_m=max_distance,
                maximum_area_difference_m2=max_area_delta,
            ),
            national_import_resumed=False,
            limitations="Identical bounded sample and settings; warm medians exclude setup and parsing. "
            "Native PG15/macOS versus emulated PG17/Linux, with different GEOS/PROJ libraries. "
            "Not an isolated architecture comparison or full national working-set forecast.",
        )
        (output / "report.json").write_text(json.dumps(report, indent=2, default=str))
        return report
    finally:
        for label in created:
            with psycopg.connect(admins[label], autocommit=True) as c:
                c.execute(sql.SQL("DROP DATABASE {} WITH(FORCE)").format(sql.Identifier(name)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--native-admin-url", required=True)
    parser.add_argument("--container-admin-url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--rows", type=int, default=131072)
    run(parser.parse_args())
