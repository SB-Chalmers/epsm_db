"""Capture a read-only, streamed public baseline and verify a disposable restore."""

import argparse
import gzip
import hashlib
import json
import subprocess
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo


def capture(url, output, container):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    info = conninfo_to_dict(url)
    database = info["dbname"]
    dump = output / "database.dump"
    with dump.open("xb") as stream:
        subprocess.run(
            [
                "docker",
                "exec",
                container,
                "pg_dump",
                "-Fc",
                "--no-owner",
                "-U",
                info["user"],
                "-d",
                database,
            ],
            stdout=stream,
            check=True,
        )
    manifest = {
        "database": database,
        "dataset": "uddevalla",
        "views": {},
        "dump_sha256": hashlib.file_digest(dump.open("rb"), "sha256").hexdigest(),
    }
    with psycopg.connect(url, options="-c default_transaction_read_only=on") as c:
        views = [
            r[0]
            for r in c.execute(
                "SELECT table_name FROM information_schema.views WHERE table_schema='mart' AND table_name NOT LIKE '%_current' ORDER BY table_name"
            )
        ]
        for name in views:
            columns = [
                r[0]
                for r in c.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='mart' AND table_name=%s ORDER BY ordinal_position",
                    (name,),
                )
            ]
            if "dataset_id" not in columns:
                continue
            count = 0
            with gzip.open(output / f"{name}.jsonl.gz", "wt", encoding="utf-8") as stream:
                with c.cursor(name="baseline_" + uuid4().hex) as cursor:
                    cursor.execute(
                        sql.SQL("SELECT to_jsonb(t) FROM mart.{} t WHERE dataset_id=%s").format(
                            sql.Identifier(name)
                        ),
                        ("uddevalla",),
                    )
                    for row in cursor:
                        stream.write(json.dumps(row[0], ensure_ascii=False, sort_keys=True) + "\n")
                        count += 1
            manifest["views"][name] = {"rows": count, "columns": columns}
        manifest["database_bytes"] = c.execute(
            "SELECT pg_database_size(current_database())"
        ).fetchone()[0]
        manifest["field_mappings"] = c.execute(
            "SELECT to_jsonb(m) FROM catalog.field_mapping m ORDER BY regime,source_field"
        ).fetchall()
    admin_info = dict(info, dbname="postgres")
    admin = make_conninfo(**admin_info)
    restore = "ubem_test_baseline_" + uuid4().hex
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(restore)))
    try:
        with dump.open("rb") as stream:
            subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    container,
                    "pg_restore",
                    "--no-owner",
                    "-U",
                    info["user"],
                    "-d",
                    restore,
                ],
                stdin=stream,
                check=True,
                capture_output=True,
            )
        with psycopg.connect(make_conninfo(**dict(info, dbname=restore))) as c:
            count = c.execute(
                "SELECT count(*) FROM mart.building_facts WHERE dataset_id='uddevalla'"
            ).fetchone()[0]
            assert count == manifest["views"]["building_facts"]["rows"]
        manifest["restore_verified"] = True
    finally:
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(restore)))
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    return {
        "output": str(output),
        "restore_verified": True,
        "views": {k: v["rows"] for k, v in manifest["views"].items()},
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--database-url", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--container", default="sweden_building_combined-postgis-1")
    a = p.parse_args()
    print(json.dumps(capture(a.database_url, a.output, a.container), indent=2))
