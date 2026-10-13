"""Read-only resource snapshots for the local national import benchmark."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess

import psycopg


def snapshot(url, filesystem, pid=None):
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "host_free_bytes": shutil.disk_usage(filesystem).free,
    }
    if pid:
        process = (
            subprocess.run(
                ["ps", "-o", "rss=,etime=", "-p", str(pid)],
                capture_output=True,
                text=True,
                check=True,
            )
            .stdout.strip()
            .split(maxsplit=1)
        )
        if process:
            result["importer_rss_bytes"] = int(process[0]) * 1024
            result["importer_elapsed"] = process[1]
    with psycopg.connect(url, options="-c default_transaction_read_only=on") as c:
        result["database_bytes"] = c.execute(
            "SELECT pg_database_size(current_database())"
        ).fetchone()[0]
        result["source_state"] = c.execute(
            "SELECT dataset_id,sealed FROM catalog.dataset WHERE dataset_kind='national_source'"
        ).fetchall()
        result["layers"] = c.execute(
            "SELECT layer,loading_status FROM catalog.dataset_layer WHERE dataset_id='national' ORDER BY layer"
        ).fetchall()
        result["work"] = c.execute(
            "SELECT stage,state,count(*),sum(rows_written)::bigint FROM catalog.work_unit GROUP BY stage,state ORDER BY stage,state"
        ).fetchall()
        result["relations"] = c.execute(
            "SELECT n.nspname||'.'||r.relname,pg_table_size(r.oid),pg_indexes_size(r.oid) FROM pg_class r JOIN pg_namespace n ON n.oid=r.relnamespace WHERE r.relkind='r' AND n.nspname IN ('catalog','raw_lm','raw_height','raw_epc','raw_scb','stock','evidence','context','scope','quality') ORDER BY pg_table_size(r.oid)+pg_indexes_size(r.oid) DESC"
        ).fetchall()
        result["temporary_io"] = c.execute(
            "SELECT temp_files,temp_bytes FROM pg_stat_database WHERE datname=current_database()"
        ).fetchone()
        result["server_wal_bytes"] = str(
            c.execute("SELECT wal_bytes FROM pg_stat_wal").fetchone()[0]
        )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--filesystem", default=".")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = snapshot(args.database_url, args.filesystem, args.pid)
    with args.output.open("a") as stream:
        stream.write(json.dumps(result) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k not in {"relations", "layers"}}))
