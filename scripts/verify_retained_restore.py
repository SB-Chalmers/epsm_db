"""Restore the retained dump and verify exact public rows against full EWKB baseline."""
from collections import Counter
import gzip
import hashlib
import json
import subprocess
from uuid import uuid4

import psycopg
from psycopg import sql

from scripts.compare_public_baseline import public_rows_statement
from scripts.preserve_uddevalla import DEST, checksum, digest, verify


def row_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).digest()


def verify_restore():
    verify()
    baseline = DEST / "baseline/full-geometry"
    manifest = json.loads((baseline / "manifest.json").read_text())
    dump = DEST / "baseline/original/database.dump"
    if digest(dump) != manifest["dump_sha256"]:
        raise ValueError("Retained dump differs from historical manifest")
    container = "sweden_building_combined-postgis-1"
    restored = "ubem_test_retention_" + uuid4().hex
    subprocess.run(["docker", "exec", container, "createdb", "-U", "ubem_owner", restored], check=True)
    result = {"dump_sha256": digest(dump), "database": restored, "views": {},
              "exact_public_values": True}
    try:
        with dump.open("rb") as stream:
            subprocess.run(["docker", "exec", "-i", container, "pg_restore", "--exit-on-error",
                            "--no-owner", "-U", "ubem_owner", "-d", restored], stdin=stream, check=True)
        for database in (restored, "sweden_ubem"):
            with psycopg.connect(dbname=database, user="ubem_owner", password="ubem-local-only",
                                 host="127.0.0.1", port=55432,
                                 options="-c default_transaction_read_only=on") as c:
                verified = {}
                for name, metadata in manifest["views"].items():
                    before = Counter()
                    with gzip.open(baseline / f"{name}.jsonl.gz", "rt") as stream:
                        for line in stream:
                            before[row_hash(json.loads(line))] += 1
                    count = 0
                    with c.cursor(name="retention_" + uuid4().hex) as cursor:
                        cursor.execute(public_rows_statement(c, name), ("uddevalla",))
                        for (value,) in cursor:
                            before[row_hash(value)] -= 1
                            count += 1
                    if count != metadata["rows"] or any(before.values()):
                        raise ValueError(f"Exact baseline mismatch: {database} mart.{name}")
                    verified[name] = count
                    print(f"Verified {database} mart.{name}: {count}", flush=True)
                mappings = [row[0] for row in c.execute(
                    "SELECT to_jsonb(m) FROM catalog.field_mapping m ORDER BY regime,source_field")]
                if mappings != [row[0] for row in manifest["field_mappings"]]:
                    raise ValueError("Retained field mappings differ")
                result["views"][database] = verified
        result["restore_verified"] = True
    finally:
        subprocess.run(["docker", "exec", container, "dropdb", "-U", "ubem_owner", restored], check=True)
    (DEST / "restore-verification.json").write_text(json.dumps(result, indent=2) + "\n")
    checksum()
    return result


if __name__ == "__main__":
    print(json.dumps(verify_restore(), indent=2))
