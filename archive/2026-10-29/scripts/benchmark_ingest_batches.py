"""Bounded COPY benchmark in a disposable schema; shared evidence is read-only.

Keep the real national parent foreign keys on benchmark tables. Compare batching
and connection reuse without resuming import or changing published/source rows.
This is a warm-cache sample, not a national-runtime forecast.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from sweden_ubem.jobs import bulk_insert


def benchmark(url, output, limit):
    schema = "ubem_ingest_bench_" + uuid4().hex
    with psycopg.connect(url, row_factory=dict_row) as c:
        c.execute("SET TRANSACTION READ ONLY")
        records = c.execute(
            "SELECT * FROM stock.component_source_feature TABLESAMPLE SYSTEM(1) "
            "REPEATABLE(42) LIMIT %s",
            (limit,),
        ).fetchall()
    if not records:
        raise ValueError("No imported lineage sample available")
    results = []
    try:
        with psycopg.connect(url) as c:
            c.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        # Alternate ordering so the result is not just the first run's cold cache.
        for repeat, variants in enumerate(
            [
                [(1024, False), (8192, True), (1024, True), (8192, False)],
                [(8192, False), (1024, True), (8192, True), (1024, False)],
            ]
        ):
            for size, reuse in variants:
                table = f"{schema}.sample"
                with psycopg.connect(url) as c:
                    c.execute(
                        sql.SQL(
                            "CREATE TABLE {} (LIKE stock.component_source_feature INCLUDING ALL)"
                        ).format(sql.Identifier(schema, "sample"))
                    )
                    c.execute(
                        sql.SQL(
                            "ALTER TABLE {} ADD FOREIGN KEY(dataset_id,component_id) "
                            "REFERENCES stock.geometry_component(dataset_id,component_id), "
                            "ADD FOREIGN KEY(source_version_id) "
                            "REFERENCES catalog.source_version(source_version_id)"
                        ).format(sql.Identifier(schema, "sample"))
                    )
                shared = psycopg.connect(url) if reuse else None
                started = perf_counter()
                try:
                    for offset in range(0, len(records), size):
                        c = shared or psycopg.connect(url)
                        try:
                            with c.transaction():
                                bulk_insert(c, table, records[offset : offset + size])
                        finally:
                            if shared is None:
                                c.close()
                finally:
                    if shared:
                        shared.close()
                seconds = perf_counter() - started
                result = dict(
                    repeat=repeat,
                    batch_rows=size,
                    reuse_connection=reuse,
                    rows=len(records),
                    seconds=seconds,
                    rows_per_second=len(records) / seconds,
                )
                results.append(result)
                print(json.dumps(result), flush=True)
                with psycopg.connect(url) as c:
                    actual = c.execute(
                        sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(schema, "sample"))
                    ).fetchone()[0]
                    if actual != len(records):
                        raise RuntimeError("Benchmark row count mismatch")
                    c.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(schema, "sample")))
    finally:
        with psycopg.connect(url) as c:
            c.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "national_import_resumed": False,
        "source_or_public_rows_modified": False,
        "parent_foreign_keys_preserved": True,
        "limitations": "Warm-cache sample; excludes source parsing, work audit, and full import indexes.",
        "results": results,
    }
    Path(output).write_text(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--rows", type=int, default=32768)
    args = parser.parse_args()
    benchmark(args.database_url, args.output, args.rows)
