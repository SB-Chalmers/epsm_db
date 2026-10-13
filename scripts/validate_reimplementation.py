"""Run Uddevalla acceptance after the separate national import finishes.

Never replaces the working database. Stop for review if the semantic comparator
reports differences; successful processing alone is not functional acceptance.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

import psycopg

from compare_public_baseline import compare
from classify_parity import classify
from sweden_ubem.database import application_digest
from sweden_ubem.selection import process_dataset
from sweden_ubem.exports import export_dataset
from sweden_ubem.epc_reporting import epc_report
from sweden_ubem.physical_assessment import assess


@contextmanager
def disconnected_sources(root):
    from sweden_ubem.asset_operations import rename_exclusive
    root=Path(root).resolve()
    hidden=root.with_name(root.name+'.acceptance-disconnected')
    if hidden.exists():raise FileExistsError('Disconnected source destination already exists')
    if not root.is_dir():raise FileNotFoundError(root)
    previous=os.environ.get('UBEM_SOURCE_ROOT')
    rename_exclusive(root,hidden)
    os.environ['UBEM_SOURCE_ROOT']=str(root)
    try:
        assert not root.exists()
        yield hidden
    finally:
        rename_exclusive(hidden,root)
        if previous is None:os.environ.pop('UBEM_SOURCE_ROOT',None)
        else:os.environ['UBEM_SOURCE_ROOT']=previous


def validate(args):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    code_digest = application_digest()

    def report(stage, **details):
        status = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "dataset": "uddevalla",
            "municipality": "1485",
            "code_digest": code_digest,
            **details,
        }
        temporary = output / "status.tmp"
        temporary.write_text(json.dumps(status, indent=2, default=str))
        temporary.replace(output / "status.json")
        print(json.dumps(status, default=str), flush=True)

    try:
        report("waiting_for_national_source_import")
        while True:
            with psycopg.connect(
                args.database_url, options="-c default_transaction_read_only=on"
            ) as c:
                source = c.execute(
                    "SELECT sealed FROM catalog.dataset "
                    "WHERE dataset_id='national' AND dataset_kind='national_source'"
                ).fetchone()
            if source and source[0]:
                break
            if not args.importer_pid:
                raise RuntimeError("National source import is not sealed")
            try:
                os.kill(args.importer_pid, 0)
            except ProcessLookupError as error:
                raise RuntimeError(
                    "Importer exited before source completion; inspect import log"
                ) from error
            time.sleep(30)
        if application_digest() != code_digest:
            raise RuntimeError(
                "Processing code changed while waiting; restart acceptance with reviewed code"
            )
        from sweden_ubem.source_inventory import DEFAULT_SOURCE_ROOT
        root=getattr(args,'source_root',None) or os.environ.get('UBEM_SOURCE_ROOT') or DEFAULT_SOURCE_ROOT
        with disconnected_sources(root) as moved_root:
            report("processing_uddevalla")
            result = process_dataset(
                args.database_url,
                dataset="uddevalla",
                municipality="1485",
                assessment_date="2026-10-12",
            )
            (output / "processing.json").write_text(json.dumps(result, indent=2, default=str))
            if not result["validation"]["valid"]:
                raise RuntimeError("Uddevalla processing did not pass dataset validation")
            report("comparing_uddevalla_baseline")
            parity = compare(args.database_url, args.baseline, output / "parity", "uddevalla", source_root=moved_root)
            classification = classify(output / "parity", args.database_url)
            report("exporting_uddevalla")
            exports = {
                kind: export_dataset(
                    args.database_url, "uddevalla", format=kind, output=output / "exports" / kind
                )
                for kind in ("csv", "geoparquet", "geopackage")
            }
            (output / "exports.json").write_text(json.dumps(exports, indent=2, default=str))
            epc_report(args.database_url, "uddevalla", output / "epc-report")
            report("assessing_height_storeys_and_area")
            physical = assess(args.database_url)
            (output / "physical-assessment.json").write_text(
                json.dumps(physical, indent=2, default=str)
            )
            with psycopg.connect(args.database_url, options="-c default_transaction_read_only=on") as c:
                c.execute("SET ROLE ubem_readonly")
                assert (
                    c.execute(
                        "SELECT count(*) FROM mart.building_facts WHERE dataset_id='uddevalla'"
                    ).fetchone()[0]
                    > 0
                )
            report("executing_exploration_notebook")
            import nbformat
            from nbclient import NotebookClient

            notebook = nbformat.read("notebooks/explore_uddevalla.ipynb", as_version=4)
            os.environ["UBEM_DATABASE_URL"] = args.database_url
            os.environ["UBEM_NOTEBOOK_DATASET"] = "uddevalla"
            NotebookClient(notebook, timeout=600, kernel_name="python3").execute()
            nbformat.write(notebook, output / "explore_uddevalla.executed.ipynb")
            report(
                "acceptance_passed" if classification["accepted"] else "parity_review_required",
                equal_functional_values=parity["equal_functional_values"],
                exports_verified=True,
                ordinary_consumers_verified_without_raw_root=True,
                readonly_role_verified=True,
                notebook_verified=True,
                physical_assessment="physical-assessment.json",
                parity_classification=classification,
            )
            return 0 if classification["accepted"] else 1
    except Exception as error:
        report("attention_required", error=str(error))
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--importer-pid", type=int)
    parser.add_argument("--source-root",type=Path)
    raise SystemExit(validate(parser.parse_args()))
