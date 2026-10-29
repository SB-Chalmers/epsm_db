"""Supervise national import followed by Uddevalla acceptance as one local job.

Run through the operating system's job supervisor for persistence beyond a chat
terminal session. A failure stops the job and leaves resumable database checkpoints.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import shutil
import time

from sweden_ubem.cli import main
from sweden_ubem.database import application_digest
from validate_reimplementation import validate
from measure_ingestion import snapshot
from reconcile_national_sources import reconcile


def run(args):
    os.chdir(Path(__file__).resolve().parents[1])
    control=Path('output/restart-2026-10-13/national-run-control.json')
    if control.exists() and json.loads(control.read_text()).get('paused'):
        raise RuntimeError('National ingestion is paused by the user; explicit resume is required')
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    performance=json.loads(Path(args.performance_report).read_text())
    if not performance.get('national_restart_accepted'):
        raise ValueError('Measured performance and capacity acceptance is required')
    required=int(performance['capacity_requirement_bytes'])
    if shutil.disk_usage(output).free<required:
        raise ValueError('Available disk is below the accepted full-run capacity requirement')
    code_digest = application_digest()
    if performance.get('replacement_code_digest')!=code_digest:
        raise ValueError('Implementation changed after measured acceptance')
    stop = threading.Event()
    current = {"stage": "starting"}

    def heartbeat():
        while not stop.wait(30):
            value = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "supervisor_pid": os.getpid(),
                "stage": current["stage"],
                "free_bytes": shutil.disk_usage(output).free,
                "elapsed_seconds": time.monotonic() - started,
            }
            try:
                resources = snapshot(args.database_url, Path.cwd(), os.getpid())
                with (output / "resources.jsonl").open("a") as stream:
                    stream.write(json.dumps(resources) + "\n")
                value.update(
                    database_bytes=resources["database_bytes"],
                    importer_rss_bytes=resources.get("importer_rss_bytes"),
                    work=resources["work"],
                )
            except Exception as error:
                value["monitor_error"] = str(error)
            temporary = output / "heartbeat.tmp"
            temporary.write_text(json.dumps(value, indent=2))
            temporary.replace(output / "heartbeat.json")
            print(json.dumps(value), flush=True)

    started = time.monotonic()
    threading.Thread(target=heartbeat, daemon=True).start()

    def report(stage, **details):
        current["stage"] = stage
        status = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "supervisor_pid": os.getpid(),
            "code_digest": code_digest,
            "processing_target": "uddevalla",
            "municipality": "1485",
            **details,
        }
        temporary = output / "supervisor-status.tmp"
        temporary.write_text(json.dumps(status, indent=2))
        temporary.replace(output / "supervisor-status.json")
        print(json.dumps(status), flush=True)

    try:
        report("resuming_national_source_import")
        result = main(
            [
                "db",
                "init",
                "--database-url",
                args.database_url,
                "--source-bundle",
                args.source_bundle,
                "--resume",
                "--source-root",
                str(args.source_root),
            ]
        )
        if result:
            raise RuntimeError("National source initialization failed")
        if application_digest() != code_digest:
            raise RuntimeError("Application code changed during import; restart with reviewed code")
        report("independent_national_reconciliation")
        reconcile(args.database_url,args.source_root,output/'national-reconciliation.json')
        report("running_uddevalla_acceptance")
        result = validate(
            argparse.Namespace(
                database_url=args.database_url,
                baseline=args.baseline,
                output=args.output,
                importer_pid=None,
                source_root=args.source_root,
            )
        )
        report("uddevalla_acceptance_passed" if result == 0 else "uddevalla_parity_review_required")
        return result
    except BaseException as error:
        report("attention_required", error=str(error))
        raise
    finally:
        stop.set()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--source-bundle", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--source-root",type=Path,required=True)
    parser.add_argument("--performance-report",type=Path,required=True)
    parser.add_argument("--output", required=True)
    raise SystemExit(run(parser.parse_args()))
