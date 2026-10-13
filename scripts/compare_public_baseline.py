"""Disk-backed multiset comparison of saved public rows and a replacement database.

Compare functional values, geometry and provenance bytes. Generated execution and
record identifiers may change across an explicitly clean reimplementation. Catalog
inventory/run projections are reported separately because national import changes
their scope. Every functional difference is written, including support expansion.
"""

import argparse
import gzip
import hashlib
import json
import sqlite3
import zlib
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
import shapely
from contextvars import ContextVar

TRANSLATIONS = ContextVar("baseline_source_path_translations",default={})
PATH_FIELDS = {"file","path","source_file","source_path","source_document","evidence_record_path","manifest_path","reconciliation_path","geometry_register_root","lineage_path","register_path","catalogue_path"}

BOOKKEEPING = {"datasets", "process_runs", "source_inventory"}
GENERATED = {"run_id", "register_run_id", "record_id", "finding_id", "source_version_id"}


def public_rows_statement(c, name):
    """Preserve full geometry precision; PostGIS's JSON cast uses rounded GeoJSON."""
    geometry = [
        r[0]
        for r in c.execute(
            "SELECT a.attname FROM pg_attribute a JOIN pg_type t ON t.oid=a.atttypid "
            "WHERE a.attrelid=%s::regclass AND a.attnum>0 AND NOT a.attisdropped AND t.typname='geometry'",
            ("mart." + name,),
        )
    ]
    extra = sql.SQL("")
    if geometry:
        entries = []
        for column in geometry:
            entries.extend(
                [
                    sql.Literal(column),
                    sql.SQL("encode(ST_AsEWKB({},'XDR'),'hex')").format(sql.Identifier(column)),
                ]
            )
        extra = sql.SQL(" || jsonb_build_object({})").format(sql.SQL(",").join(entries))
    return sql.SQL("SELECT to_jsonb(t){} FROM mart.{} t WHERE dataset_id=%s").format(
        extra, sql.Identifier(name)
    )


def canonical(value, path=()):
    if isinstance(value, dict):
        return {
            k: canonical(v, (*path, k))
            for k, v in value.items()
            if "original_values" in path or k not in GENERATED
        }
    if isinstance(value, list):
        # JSON arrays can be ordered evidence, so preserve their order.
        return [canonical(v, path) for v in value]
    if isinstance(value,str) and path and path[-1] in PATH_FIELDS:
        return TRANSLATIONS.get().get(value,value)
    if isinstance(value, str) and path and path[-1] == "geom":
        geom = shapely.from_wkb(bytes.fromhex(value))
        return shapely.to_wkb(shapely.normalize(geom)).hex()
    return value


def add(store, view, values, side):
    buffered = []
    count = 0
    statement = f"INSERT INTO comparison(view,digest,payload,{side}) VALUES(?,?,?,1) ON CONFLICT(view,digest) DO UPDATE SET {side}={side}+1"
    for value in values:
        data = json.dumps(
            canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        buffered.append((view, hashlib.sha256(data).digest(), zlib.compress(data)))
        count += 1
        if len(buffered) == 2048:
            store.executemany(statement, buffered)
            buffered.clear()
    if buffered:
        store.executemany(statement, buffered)
    store.commit()
    return count


def compare(url, baseline, output, dataset="uddevalla",source_root=None):
    baseline, output = Path(baseline), Path(output)
    output.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((baseline / "manifest.json").read_text())
    from sweden_ubem.source_inventory import DEFAULT_SOURCE_ROOT
    import os
    root=Path(source_root or os.environ.get("UBEM_SOURCE_ROOT") or DEFAULT_SOURCE_ROOT)
    translations={}
    inventory=root/"manifests/file-inventory.json"
    if inventory.is_file():
        for item in json.loads(inventory.read_text())["files"]:
            logical="bundle:"+item["path"]
            translations[item["original"]]=logical
            translations[str(root/item["path"])]=logical
            translations[str(DEFAULT_SOURCE_ROOT/item["path"])]=logical
    TRANSLATIONS.set(translations)
    report = {
        "dataset": dataset,
        "baseline": str(baseline),
        "ignored_generated_fields": sorted(GENERATED),
        "geometry_comparison": "normalized WKB; retain coordinates",
        "float_precision": "exact; only keyed documented derived-geometry equivalence may be classified",
        "source_path_translation": "exact registered paths in declared path fields",
        "bookkeeping_views": {},
        "functional_views": {},
    }
    cache = output / "comparison.sqlite"
    with (
        sqlite3.connect(cache) as store,
        psycopg.connect(url, options="-c default_transaction_read_only=on") as c,
    ):
        store.execute("PRAGMA journal_mode=WAL")
        store.execute(
            "CREATE TABLE comparison(view TEXT,digest BLOB,payload BLOB,baseline INTEGER NOT NULL DEFAULT 0,current INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(view,digest)) WITHOUT ROWID"
        )
        for name, metadata in manifest["views"].items():
            columns = [
                r[0]
                for r in c.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='mart' AND table_name=%s ORDER BY ordinal_position",
                    (name,),
                )
            ]
            if name in BOOKKEEPING:
                current = c.execute(
                    sql.SQL("SELECT count(*) FROM mart.{} WHERE dataset_id=%s").format(
                        sql.Identifier(name)
                    ),
                    (dataset,),
                ).fetchone()[0]
                report["bookkeeping_views"][name] = {
                    "baseline_rows": metadata["rows"],
                    "current_rows": current,
                }
                continue
            with gzip.open(baseline / f"{name}.jsonl.gz", "rt") as stream:
                before = add(store, name, (json.loads(line) for line in stream), "baseline")
            with c.cursor(name="parity_" + uuid4().hex) as cursor:
                cursor.execute(
                    public_rows_statement(c, name),
                    (dataset,),
                )
                after = add(store, name, (row[0] for row in cursor), "current")
            missing, added = store.execute(
                "SELECT COALESCE(sum(max(baseline-current,0)),0),COALESCE(sum(max(current-baseline,0)),0) FROM comparison WHERE view=?",
                (name,),
            ).fetchone()
            report["functional_views"][name] = {
                "baseline_rows": before,
                "current_rows": after,
                "missing_rows": missing,
                "added_rows": added,
                "removed_columns": sorted(set(metadata["columns"]) - set(columns)),
                "added_columns": sorted(set(columns) - set(metadata["columns"])),
            }
            if missing or added:
                with gzip.open(output / f"{name}-differences.jsonl.gz", "wt") as stream:
                    for payload, old_count, new_count in store.execute(
                        "SELECT payload,baseline,current FROM comparison WHERE view=? AND baseline<>current",
                        (name,),
                    ):
                        stream.write(
                            json.dumps(
                                {
                                    "baseline_count": old_count,
                                    "current_count": new_count,
                                    "row": json.loads(zlib.decompress(payload)),
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
            store.execute("DELETE FROM comparison WHERE view=?", (name,))
            store.commit()
            print(name, before, after, missing, added, flush=True)
    # sqlite's connection context commits but does not close the connection.
    # Close before removing its cache so WAL/SHM cannot remain as large orphans.
    store.close()
    report["equal_functional_values"] = all(
        not (r["missing_rows"] or r["added_rows"] or r["removed_columns"] or r["added_columns"])
        for r in report["functional_views"].values()
    )
    (output / "report.json").write_text(json.dumps(report, indent=2))
    cache.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(cache) + suffix).unlink(missing_ok=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--dataset", default="uddevalla")
    parser.add_argument("--source-root",type=Path)
    args = parser.parse_args()
    compare(args.database_url, args.baseline, args.output, args.dataset,args.source_root)
