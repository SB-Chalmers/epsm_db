"""Remove only the verified disposable v003 database and its dedicated cluster."""
import json
import os
import plistlib
import subprocess
from pathlib import Path

import psycopg
from sweden_ubem.asset_operations import deletion_manifest, delete_listed, publish_json
from scripts.preserve_uddevalla import ROOT, DEST, verify

RUNTIME = ROOT / "output/runtime/native-pg17"
DIAGNOSTICS = ROOT / "output/restart-2026-10-13"
PGBIN = Path("/opt/homebrew/opt/postgresql@17/bin")


def retire():
    verify()
    restore = json.loads((DEST / "restore-verification.json").read_text())
    if not restore.get("restore_verified") or not restore.get("exact_public_values"):
        raise ValueError("Verified preservation is required before retirement")
    processes = subprocess.run(["ps", "ax", "-o", "pid=,command="], check=True,
                               capture_output=True, text=True).stdout.splitlines()
    hazards = [p for p in processes if any(word in p for word in
               ("run_reimplementation", "sweden_ubem db init", "sweden-ubem db init"))]
    agents = []
    for directory in (Path.home() / "Library/LaunchAgents", Path("/Library/LaunchAgents"),
                      Path("/Library/LaunchDaemons")):
        for path in directory.glob("*.plist"):
            values = plistlib.loads(path.read_bytes())
            if any(word in str(values) for word in (str(RUNTIME), "run_reimplementation", "sweden_ubem_v003")):
                agents.append(str(path))
    live_labels = subprocess.run(["launchctl", "list"], check=True, capture_output=True,
                                 text=True).stdout
    if hazards or agents or any(word in live_labels.lower() for word in ("ubem", "reimplementation")):
        raise ValueError(f"Potential ingestion restart mechanism: {hazards} {agents}")
    with psycopg.connect(host="/tmp", port=55433, user="ubem_owner", dbname="postgres",
                         autocommit=True) as c:
        port, data, identifier = c.execute(
            "SELECT current_setting('port'),current_setting('data_directory'),"
            "(pg_control_system()).system_identifier::text").fetchone()
        if port != "55433" or Path(data).resolve() != (RUNTIME / "data").resolve():
            raise ValueError("Native cluster identity mismatch")
        names = [r[0] for r in c.execute("SELECT datname FROM pg_database WHERE NOT datistemplate AND datname<>'postgres'")]
        if names != ["sweden_ubem_v003"]:
            raise ValueError(f"Unexpected application databases: {names}")
        if c.execute("SELECT count(*) FROM pg_stat_activity WHERE datname='sweden_ubem_v003'").fetchone()[0]:
            raise ValueError("Disposable database has active clients")
        with psycopg.connect(host="/tmp", port=55433, user="ubem_owner", dbname="sweden_ubem_v003",
                             options="-c default_transaction_read_only=on") as source:
            layers = source.execute("SELECT layer,loading_status FROM catalog.dataset_layer ORDER BY layer").fetchall()
            work = source.execute("SELECT stage,state,count(*),sum(rows_written)::bigint FROM catalog.work_unit GROUP BY stage,state ORDER BY stage,state").fetchall()
            failures = source.execute("SELECT layer,error FROM catalog.process_run WHERE status='failed' ORDER BY started_at DESC LIMIT 10").fetchall()
            size = source.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        diagnostic = {"database": "sweden_ubem_v003", "port": port, "data_directory": data,
                      "system_identifier": identifier, "bytes": size, "layers": layers,
                      "work": work, "failures": failures, "restart_mechanisms": [],
                      "protected": ["Docker sweden_ubem:55432", "Homebrew PostgreSQL 15"]}
        publish_json(DIAGNOSTICS / "rejected-v003.json", diagnostic)
        # Recheck immediately before preventing new clients and removing this database.
        if c.execute("SELECT (pg_control_system()).system_identifier::text").fetchone()[0] != identifier:
            raise ValueError("Cluster identity changed")
        c.execute("ALTER DATABASE sweden_ubem_v003 ALLOW_CONNECTIONS false")
        c.execute("DROP DATABASE sweden_ubem_v003")
        if c.execute("SELECT count(*) FROM pg_database WHERE NOT datistemplate AND datname<>'postgres'").fetchone()[0]:
            raise ValueError("Other application databases prevent runtime removal")
    subprocess.run([str(PGBIN / "pg_ctl"), "-D", str(RUNTIME / "data"), "-m", "fast", "-w", "stop"], check=True)
    manifest = deletion_manifest([RUNTIME], protected=[DEST, ROOT / "data/raw"], checksum=False)
    publish_json(DIAGNOSTICS / "runtime-deletion-manifest.json", manifest)
    delete_listed(manifest, [RUNTIME])
    diagnostic["removed"] = True
    publish_json(DIAGNOSTICS / "rejected-v003.json", diagnostic)
    return {"removed_database": "sweden_ubem_v003", "removed_runtime": str(RUNTIME),
            "reclaimed_database_bytes": size}


if __name__ == "__main__":
    print(json.dumps(retire(), indent=2))
