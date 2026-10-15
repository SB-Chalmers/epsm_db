from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from sweden_ubem._logging import configure_cli

from .database import initialize_database, rebuild_database, validate_dataset
from .dataset import LocalSources
from .source_inventory import load_source_inventory


def build_parser():
    p = argparse.ArgumentParser(prog="sweden-ubem")
    p.add_argument(
        "--log-level",
        choices=("OFF", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        default="INFO",
        help="Status output on stderr",
    )
    commands = p.add_subparsers(dest="command", required=True)
    db = commands.add_parser("db").add_subparsers(dest="action", required=True)
    db.add_parser("status").add_argument("--database-url")
    for name in ("init", "rebuild"):
        c = db.add_parser(name)
        c.add_argument("--database-url")
        if name == "init":
            c.add_argument(
                "--resume",
                action="store_true",
                help="Resume verified completed source work (also the default)",
            )
            c.add_argument(
                "--source-bundle",
                type=Path,
                default=Path(__file__).resolve().parents[2] / "config/national-source-bundle.toml",
            )
            c.add_argument("--profile")
            c.add_argument("--source-root")
            c.add_argument("--batch-rows", type=int)
            c.add_argument("--batch-bytes", type=int)
            c.add_argument(
                "--schema-only",
                action="store_true",
                help="Create empty schema for tests or staged source setup",
            )
        if name == "rebuild":
            c.add_argument("--confirm-destroy", action="store_true")
            c.add_argument("--expected-database", required=True)
            c.add_argument("--backup", type=Path, required=True)
            c.add_argument("--pg-dump")
            c.add_argument("--backup-container")
    dataset = commands.add_parser("dataset").add_subparsers(dest="action", required=True)
    for name in ("plan", "process", "load", "validate", "report-epc"):
        c = dataset.add_parser(name)
        c.add_argument("--dataset", default="uddevalla")
        c.add_argument("--municipality", default="1485")
        c.add_argument("--database-url")
        c.add_argument("--profile")
        c.add_argument("--source-root")
        c.add_argument("--source", default="national")
        c.add_argument("--national", action="store_true")
        if name in ("load", "process"):
            c.add_argument("--layers", nargs="+")
            c.add_argument(
                "--assessment-date", help="ISO date for EPC temporal diagnostics; defaults to today"
            )
            c.add_argument("--site-event-source", action="append")
        if name == "report-epc":
            c.add_argument("--output", type=Path, required=True)
        if name == "validate":
            c.add_argument("--compare-oracle", type=Path)
            c.add_argument("--output", type=Path)
    source = commands.add_parser("source").add_subparsers(dest="action", required=True)
    inspect = source.add_parser("inspect")
    inspect.add_argument("--record-id", required=True)
    inspect.add_argument("--database-url")
    inspect.add_argument("--source-root")
    for name in ("inventory", "validate", "register", "acquire-scb"):
        c = source.add_parser(name)
        c.add_argument("--profile")
        c.add_argument("--source-root")
        c.add_argument("--verify", action="store_true")
        if name == "register":
            c.add_argument("--database-url")
            c.add_argument("--layer", required=True)
            c.add_argument("files", type=Path, nargs="+")
        if name == "acquire-scb":
            c.add_argument("--municipality", default="1485")
            c.add_argument("--population-grid", type=Path)
            c.add_argument("--output", type=Path)
            c.add_argument("--regso-year", type=int, default=2025)
            c.add_argument("--income-year", type=int, default=2024)
    export = commands.add_parser("export")
    export.add_argument("--format", choices=["geopackage", "geoparquet", "csv"], required=True)
    export.add_argument("--dataset", default="uddevalla")
    export.add_argument("--database-url")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--building-id", action="append")
    scope = commands.add_parser("scope").add_subparsers(dest="action", required=True)
    evaluate = scope.add_parser("evaluate")
    evaluate.add_argument("--database-url")
    evaluate.add_argument("--dataset", default="uddevalla")
    evaluate.add_argument("--scope", default="all-buildings")
    evaluate.add_argument("--rules", type=Path)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    with configure_cli(a.log_level, "sweden_ubem"):
        url = getattr(a, "database_url", None) or os.environ.get("UBEM_DATABASE_URL")
        if (
            a.command in ("db", "export", "scope")
            or a.command == "dataset"
            and a.action in ("plan", "load", "process", "validate", "report-epc")
            or a.command == "source"
            and a.action in ("register", "inspect")
        ):
            if not url:
                raise ValueError("Set UBEM_DATABASE_URL or --database-url")
        if a.command == "db":
            if a.action == "status":
                from .status import database_status

                result = database_status(url)
                print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
                return 0
            result = (
                initialize_database(url)
                if a.action == "init"
                else rebuild_database(
                    url,
                    expected_database=a.expected_database,
                    backup=a.backup,
                    confirmed=a.confirm_destroy,
                    pg_dump=a.pg_dump,
                    backup_container=a.backup_container,
                )
            )
            if a.action == "init" and not a.schema_only:
                import tomllib

                from .ingest import initialize_sources

                bundle = tomllib.loads(a.source_bundle.read_text())
                result = initialize_sources(
                    url,
                    LocalSources(a.profile, a.source_root),
                    source=bundle.get("source_id", "national"),
                    bundle=bundle,
                    batch_rows=a.batch_rows,
                    batch_bytes=a.batch_bytes,
                )
        elif a.command == "dataset":
            if a.action == "report-epc":
                from .epc_reporting import epc_report

                result = epc_report(url, a.dataset, a.output)
                print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
                return 0
            from .selection import plan_dataset as database_plan
            from .selection import process_dataset

            result = (
                database_plan(url, a.dataset, None if a.national else a.municipality, a.source)
                if a.action == "plan"
                else process_dataset(
                    url,
                    dataset=a.dataset,
                    municipality=None if a.national else a.municipality,
                    source=a.source,
                    layers=a.layers,
                    assessment_date=a.assessment_date,
                    event_sources=a.site_event_source,
                )
                if a.action in ("load", "process")
                else validate_dataset(url, a.dataset)
            )
            if a.action == "validate" and a.compare_oracle:
                from .comparison import compare_oracle

                if not a.output:
                    raise ValueError("--output is required for oracle comparison")
                result["oracle_comparison"] = compare_oracle(
                    url, a.dataset, a.compare_oracle, a.output
                )
        elif a.command == "source":
            inventory = load_source_inventory()
            if a.action == "inspect":
                from .source_inspection import inspect_record

                result = inspect_record(url, a.record_id, a.source_root)
            elif a.action == "inventory":
                result = [
                    x.as_dict()
                    for x in inventory.resolve(
                        profile=a.profile, source_root=a.source_root, verify=a.verify
                    )
                ]
            elif a.action == "validate":
                result = inventory.validate(
                    profile=a.profile, source_root=a.source_root, verify=a.verify
                )
            elif a.action == "register":
                from .dataset import register_files

                version, files = register_files(
                    url, LocalSources(a.profile, a.source_root), a.layer, a.files
                )
                result = {"source_version": version, "files": files}
            else:
                from .scb import acquire_scb_snapshot

                snapshot = acquire_scb_snapshot(
                    population_grid=a.population_grid
                    or inventory.path_for(
                        "scb_population_grid_2025", profile=a.profile, source_root=a.source_root
                    ),
                    municipality=a.municipality,
                    output_root=a.output,
                    regso_year=a.regso_year,
                    income_year=a.income_year,
                )
                # Original response metadata stays on disk; do not print HTTP cookies.
                result = {
                    key: snapshot[key]
                    for key in ("snapshot_id", "path", "municipality", "ready")
                    if key in snapshot
                }
        elif a.command == "scope":
            from .scopes import evaluate_scopes

            result = evaluate_scopes(
                url, a.dataset, a.scope, json.loads(a.rules.read_text()) if a.rules else None
            )
        else:
            from .exports import export_dataset

            selected = a.building_id
            result = export_dataset(url, a.dataset, selected, format=a.format, output=a.output)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if isinstance(result, dict) and (
            result.get("valid") is False
            or any(isinstance(x, dict) and "failed" in x for x in result.get("layers", {}).values())
        ):
            return 1
        return 0
