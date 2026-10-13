"""Add lossless EWKB to the protected baseline without duplicating its backup."""

import argparse
from collections import Counter
import gzip
import hashlib
import json
import os
from pathlib import Path
import psycopg
from compare_public_baseline import canonical, public_rows_statement


def scalar_hash(row, geometry):
    value = canonical({k: v for k, v in row.items() if k not in geometry})
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).digest()


def augment(url, baseline, output):
    baseline, output = Path(baseline).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((baseline / "manifest.json").read_text())
    audit = {}
    with psycopg.connect(url, options="-c default_transaction_read_only=on") as c:
        for name in manifest["views"]:
            geometry = [
                r[0]
                for r in c.execute(
                    "SELECT a.attname FROM pg_attribute a JOIN pg_type t ON t.oid=a.atttypid WHERE a.attrelid=%s::regclass AND a.attnum>0 AND NOT a.attisdropped AND t.typname='geometry'",
                    ("mart." + name,),
                )
            ]
            destination = output / f"{name}.jsonl.gz"
            if not geometry:
                destination.symlink_to(os.path.relpath(baseline / f"{name}.jsonl.gz", output))
                continue
            before = Counter()
            with gzip.open(baseline / f"{name}.jsonl.gz", "rt") as f:
                for line in f:
                    before[scalar_hash(json.loads(line), geometry)] += 1
            after = Counter()
            count = 0
            with gzip.open(destination, "wt", encoding="utf-8") as f:
                with c.cursor(name="baseline_geometry") as cur:
                    cur.execute(public_rows_statement(c, name), ("uddevalla",))
                    for (row,) in cur:
                        after[scalar_hash(row, geometry)] += 1
                        count += 1
                        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            if before != after:
                raise RuntimeError(f"Original non-geometry values changed: {name}")
            audit[name] = {
                "rows": count,
                "non_geometry_values_unchanged": True,
                "geometry_columns": geometry,
                "sha256": hashlib.file_digest(destination.open("rb"), "sha256").hexdigest(),
            }
    (output / "database.dump").symlink_to(os.path.relpath(baseline / "database.dump", output))
    manifest.update(
        geometry_encoding="full EWKB XDR; original protected backup retained",
        original_baseline=str(baseline),
        geometry_augmentation=audit,
    )
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return audit


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--database-url", required=True)
    p.add_argument("--baseline", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    print(json.dumps(augment(a.database_url, a.baseline, a.output), indent=2))
