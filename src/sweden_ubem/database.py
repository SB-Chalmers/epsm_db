"""Small, transaction-based evidence database. No source acquisition here."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb
from .hashing import content_hash
from .version import VERSION
from .resources import resource_path

SCHEMAS = (
    "catalog",
    "raw_lm",
    "raw_epc",
    "raw_height",
    "raw_scb",
    "stock",
    "evidence",
    "context",
    "quality",
    "scope",
    "mart",
)
REBUILD_SCHEMAS = SCHEMAS + ("raw_epsm", "semantic")
LAYERS = (
    "geometry",
    "register",
    "addresses",
    "properties",
    "heights",
    "epc",
    "municipality",
    "population",
    "regso",
    "income",
    "property_linkage",
    "epc_linkage",
    "context_linkage",
)
HISTORICAL_MIGRATIONS = resource_path("sql/migrations")
MIGRATIONS = resource_path("sql/baselines/0.0.4")


class DatabaseSchemaMismatch(ValueError):
    pass


def _connect(url: str):
    return psycopg.connect(url)


def initialize_database(url: str, migrations: Path = MIGRATIONS) -> list[str]:
    files = sorted(migrations.glob("00*.sql"))
    if not files or not (migrations / "processing_indexes.sql").is_file():
        raise FileNotFoundError(f"Database baseline SQL is missing from {migrations}")
    expected = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in migrations.glob("*.sql")}
    with _connect(url) as c:
        c.execute("SELECT pg_advisory_xact_lock(hashtext('ubem-schema'))")
        present = c.execute("SELECT to_regclass('catalog.schema_migration')").fetchone()[0]
        if present:
            baseline = c.execute("SELECT to_regclass('catalog.schema_baseline')").fetchone()[0]
            if not baseline or c.execute("SELECT version FROM catalog.schema_baseline").fetchall() != [(VERSION,)]:
                raise DatabaseSchemaMismatch("Legacy database: 0.0.4 requires a fresh database; completed Uddevalla is protected")
            columns = c.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='catalog' AND table_name='schema_migration'"
            ).fetchall()
            if ("sha256",) not in columns:
                raise DatabaseSchemaMismatch(
                    "Old database: use the backed-up, confirmed rebuild command"
                )
            recorded = dict(
                c.execute("SELECT migration_name,sha256 FROM catalog.schema_migration").fetchall()
            )
            if any(expected.get(k) != v for k, v in recorded.items()):
                raise DatabaseSchemaMismatch(
                    "Migrations changed after application; explicit clean rebuild required"
                )
        else:
            recorded = {}
            if c.execute("SELECT to_regclass('stock.building')").fetchone()[0]:
                raise DatabaseSchemaMismatch("Unrecognized existing schema; rebuild explicitly")
        c.execute("CREATE SCHEMA IF NOT EXISTS catalog")
        c.execute(
            "CREATE TABLE IF NOT EXISTS catalog.schema_migration(migration_name text PRIMARY KEY,sha256 text NOT NULL,applied_at timestamptz NOT NULL DEFAULT now())"
        )
        c.execute("CREATE TABLE IF NOT EXISTS catalog.schema_baseline(version text PRIMARY KEY)")
        c.execute("INSERT INTO catalog.schema_baseline VALUES(%s) ON CONFLICT DO NOTHING", (VERSION,))
        applied = []
        for p in files:
            if p.name not in recorded:
                c.execute(p.read_text())
                c.execute(
                    "INSERT INTO catalog.schema_migration(migration_name,sha256) VALUES(%s,%s)",
                    (p.name, expected[p.name]),
                )
                applied.append(p.name)
        # Group roles: credentials/login users are provisioned separately.
        for role in ("ubem_ingest", "ubem_readonly"):
            if not c.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                c.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
        for schema in SCHEMAS:
            c.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO ubem_ingest").format(sql.Identifier(schema))
            )
            c.execute(
                sql.SQL(
                    "GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA {} TO ubem_ingest"
                ).format(sql.Identifier(schema))
            )
            c.execute(
                sql.SQL("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA {} TO ubem_ingest").format(
                    sql.Identifier(schema)
                )
            )
        c.execute("GRANT USAGE ON SCHEMA mart TO ubem_readonly")
        c.execute("GRANT SELECT ON ALL TABLES IN SCHEMA mart TO ubem_readonly")
        c.execute("GRANT DELETE ON scope.membership,scope.coverage TO ubem_ingest")
        c.execute("GRANT CREATE ON SCHEMA raw_lm TO ubem_ingest")
        c.execute(
            "GRANT DELETE ON context.building_area,scope.dataset_building,scope.dataset_certificate,catalog.work_unit TO ubem_ingest"
        )
        return applied


def install_processing_indexes(url):
    path = MIGRATIONS / "processing_indexes.sql"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with _connect(url) as c:
        c.execute("SELECT pg_advisory_xact_lock(hashtext('ubem-processing-indexes'))")
        existing = c.execute("SELECT sha256 FROM catalog.schema_migration WHERE migration_name=%s", (path.name,)).fetchone()
        if existing:
            if existing[0] != digest:
                raise DatabaseSchemaMismatch("Processing index definitions changed")
            return
        c.execute(path.read_text())
        c.execute("INSERT INTO catalog.schema_migration(migration_name,sha256) VALUES(%s,%s)", (path.name, digest))


def rebuild_database(
    url: str,
    *,
    expected_database: str,
    backup: Path,
    confirmed: bool = False,
    pg_dump: str | None = None,
    backup_container: str | None = None,
) -> dict:
    if not confirmed or not expected_database:
        raise ValueError("Explicit --confirm-destroy and --expected-database required")
    backup = backup.expanduser().resolve()
    if backup.exists():
        raise FileExistsError("Backup must be a new file, never overwrite an earlier backup")
    with _connect(url) as c:
        actual = c.execute("SELECT current_database()").fetchone()[0]
        if actual != expected_database or actual in ("postgres", "template0", "template1"):
            raise ValueError("Database target confirmation does not match a permitted database")
        external = c.execute(
            """SELECT DISTINCT dep.schema,dep.name,ref.schema,ref.name
          FROM pg_depend d
          CROSS JOIN LATERAL pg_identify_object(d.classid,d.objid,d.objsubid) dep
          CROSS JOIN LATERAL pg_identify_object(d.refclassid,d.refobjid,d.refobjsubid) ref
          WHERE ref.schema=ANY(%s) AND dep.schema IS NOT NULL AND NOT dep.schema=ANY(%s)
          AND dep.schema NOT IN ('pg_catalog','pg_toast','information_schema')
          AND d.deptype <> 'i'
          UNION
          SELECT DISTINCT n.nspname, v.relname, ref.schema, ref.name
          FROM pg_depend d
          JOIN pg_rewrite r ON d.classid='pg_rewrite'::regclass AND d.objid=r.oid
          JOIN pg_class v ON v.oid=r.ev_class
          JOIN pg_namespace n ON n.oid=v.relnamespace
          CROSS JOIN LATERAL pg_identify_object(d.refclassid,d.refobjid,d.refobjsubid) ref
          WHERE ref.schema=ANY(%s) AND NOT n.nspname=ANY(%s)
          AND n.nspname NOT IN ('pg_catalog','pg_toast','information_schema')
          """,
            (
                list(REBUILD_SCHEMAS),
                list(REBUILD_SCHEMAS),
                list(REBUILD_SCHEMAS),
                list(REBUILD_SCHEMAS),
            ),
        ).fetchall()
        if external:
            raise ValueError(f"External dependencies prevent rebuilding: {external}")
    executable = pg_dump or shutil.which("pg_dump")
    if not executable and not backup_container:
        raise RuntimeError(
            "A pg_dump compatible with the target server is required; no schemas were dropped"
        )
    info = conninfo_to_dict(url)
    env = dict(os.environ)
    for key in ("host", "port", "user", "password", "dbname", "sslmode"):
        if key in info:
            env["PG" + {"dbname": "DATABASE", "password": "PASSWORD"}.get(key, key.upper())] = info[
                key
            ]
    command = [executable, "--format=custom", "--no-owner"]
    if backup_container:
        identity_command = [
            "docker",
            "exec",
            backup_container,
            "psql",
            "-U",
            info.get("user", "ubem_owner"),
            "-d",
            expected_database,
            "-Atc",
            "SELECT (pg_control_system()).system_identifier",
        ]
        identifier = subprocess.run(
            identity_command, check=True, capture_output=True, text=True
        ).stdout.strip()
        with _connect(url) as c:
            expected_identifier = str(
                c.execute("SELECT (pg_control_system()).system_identifier").fetchone()[0]
            )
        if identifier != expected_identifier:
            raise ValueError("Backup container is not the confirmed database server")
        command = [
            "docker",
            "exec",
            backup_container,
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "-U",
            info.get("user", "ubem_owner"),
            "-d",
            expected_database,
        ]
    backup.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation; pg_dump writes to stdout, with passwords only in env.
    with backup.open("xb") as stream:
        os.chmod(backup, 0o600)
        subprocess.run(command, env=env, stdout=stream, check=True)
    if backup.stat().st_size == 0:
        raise RuntimeError("Empty backup; rebuild stopped")
    with _connect(url) as c:
        c.execute("SELECT pg_advisory_xact_lock(hashtext('ubem-schema'))")
        for schema in REBUILD_SCHEMAS:
            c.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
    return {"backup": str(backup), "database": actual, "migrations": initialize_database(url)}


def create_dataset(url: str, dataset_id: str, municipality: str | None = None) -> None:
    """Create an empty source namespace for internal schema/transaction exercises."""
    with _connect(url) as c:
        c.execute(
            "INSERT INTO catalog.dataset(dataset_id,metadata) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (dataset_id, Jsonb({"scope_note": municipality})),
        )
        for layer in LAYERS:
            c.execute(
                "INSERT INTO catalog.dataset_layer(dataset_id,layer) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                (dataset_id, layer),
            )


def insert(c, table: str, values: dict) -> None:
    schema, name = table.split(".")
    if schema not in SCHEMAS:
        raise ValueError("Not an application table")
    columns, expressions, params = [], [], []
    for key, value in values.items():
        columns.append(sql.Identifier(key))
        if key.endswith("_geom") or key == "geom":
            expressions.append(sql.SQL("ST_GeomFromWKB(%s,3006)"))
            params.append(
                None
                if value is None
                else bytes(value.wkb)
                if hasattr(value, "wkb")
                else bytes(value)
            )
        else:
            expressions.append(sql.Placeholder())
            params.append(
                Jsonb(value)
                if isinstance(value, dict) or key in ("raw_values", "source_locators")
                else value
            )
    c.execute(
        sql.SQL("INSERT INTO {}.{} ({}) VALUES({})").format(
            sql.Identifier(schema),
            sql.Identifier(name),
            sql.SQL(",").join(columns),
            sql.SQL(",").join(expressions),
        ),
        params,
    )


def application_digest():
    """Pin source code and schema even when the development version is unchanged."""
    root = Path(__file__).parent
    return content_hash(
        {
            str(p.relative_to(root.parent.parent)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted([*root.glob("*.py"), *MIGRATIONS.glob("*.sql")])
        }
    )


@contextmanager
def layer_run(
    url: str,
    dataset_id: str,
    layer: str,
    inputs: dict,
    policy: str = "source-projection-v1",
    source_version_id: str | None = None,
):
    run = uuid4()
    with _connect(url) as c:
        insert(
            c,
            "catalog.process_run",
            dict(
                run_id=run,
                dataset_id=dataset_id,
                layer=layer,
                policy_version=policy,
                code_version=f"sweden-ubem-{VERSION}:{application_digest()}",
                status="running",
                input_versions=inputs,
            ),
        )
    try:
        with _connect(url) as c:
            state = c.execute(
                "SELECT loading_status FROM catalog.dataset_layer WHERE dataset_id=%s AND layer=%s FOR UPDATE",
                (dataset_id, layer),
            ).fetchone()
            if not state:
                raise ValueError("Unknown dataset layer")
            if state[0] == "loaded":
                raise ValueError("Layer already loaded; create a new immutable dataset release")
            yield c, run
            c.execute(
                "UPDATE catalog.process_run SET status='complete',finished_at=clock_timestamp() WHERE run_id=%s",
                (run,),
            )
            c.execute(
                "UPDATE catalog.dataset_layer SET loading_status='loaded',source_version_id=%s,run_id=%s WHERE dataset_id=%s AND layer=%s",
                (source_version_id, run, dataset_id, layer),
            )
    except BaseException as exc:
        with _connect(url) as c:
            c.execute(
                "UPDATE catalog.process_run SET status='failed',finished_at=clock_timestamp(),error=%s WHERE run_id=%s",
                (str(exc), run),
            )
            c.execute(
                "UPDATE catalog.dataset_layer SET loading_status='failed' WHERE dataset_id=%s AND layer=%s AND loading_status<>'loaded'",
                (dataset_id, layer),
            )
        raise


def validate_dataset(url: str, dataset_id: str) -> dict:
    with _connect(url) as c:
        selected = c.execute(
            "SELECT source_dataset_id,processing_release_id FROM catalog.dataset WHERE dataset_id=%s AND dataset_kind<>'national_source'",
            (dataset_id,),
        ).fetchone()
        if not selected:
            return {
                "dataset": dataset_id,
                "valid": False,
                "checks": {"selection_exists": False},
                "layers": {},
            }
        source, processing = selected
        count = c.execute(
            "SELECT count(*) FROM scope.dataset_building WHERE dataset_id=%s", (dataset_id,)
        ).fetchone()[0]
        checks = {}
        for view in ("building_facts", "building_context_summary"):
            n, u = c.execute(
                sql.SQL(
                    "SELECT count(*),count(DISTINCT building_id) FROM mart.{} WHERE dataset_id=%s"
                ).format(sql.Identifier(view)),
                (dataset_id,),
            ).fetchone()
            checks[view + "_grain"] = n == u == count
        checks["height_hashes"] = c.execute(
            "SELECT count(*)=0 FROM mart.component_heights h JOIN mart.building_components g USING(dataset_id,component_id) WHERE h.dataset_id=%s AND h.input_geometry_hash<>g.geometry_hash",
            (dataset_id,),
        ).fetchone()[0]
        layers = dict(
            c.execute(
                "SELECT layer,loading_status FROM catalog.dataset_layer WHERE dataset_id=%s",
                (dataset_id,),
            )
        )
        checks["geometry_loaded"] = layers.get("geometry") == "loaded" and count > 0
        checks["dependency_signatures"] = all(
            signature == content_hash([processing, layer])
            for layer, signature in c.execute(
                "SELECT layer,input_signature FROM catalog.dataset_layer WHERE dataset_id=%s AND loading_status='loaded'",
                (dataset_id,),
            )
        )
        checks["required_layers_complete"] = all(layers.get(layer) == "loaded" for layer in LAYERS)
        if layers.get("epc_linkage") == "loaded":
            checks["epc_every_subject_assessed"] = c.execute(
                "SELECT (SELECT count(*) FROM mart.epc_subjects WHERE dataset_id=%s)=(SELECT count(*) FROM mart.epc_subject_results WHERE dataset_id=%s)",
                (dataset_id, dataset_id),
            ).fetchone()[0]
            checks["epc_outcomes_agree_with_links"] = c.execute(
                "SELECT count(*)=0 FROM mart.epc_subject_results s WHERE s.dataset_id=%s AND (s.outcome='matched')<>EXISTS(SELECT 1 FROM mart.epc_candidates r WHERE (r.dataset_id,r.subject_id)=(s.dataset_id,s.subject_id) AND r.decision='accepted')",
                (dataset_id,),
            ).fetchone()[0]
            checks["epc_candidate_membership"] = c.execute(
                "SELECT count(*)=0 FROM mart.epc_candidates r WHERE r.dataset_id=%s AND NOT EXISTS(SELECT 1 FROM scope.dataset_building m WHERE (m.dataset_id,m.building_id)=(r.dataset_id,r.building_id))",
                (dataset_id,),
            ).fetchone()[0]
        if layers.get("context_linkage") == "loaded":
            checks["context_jobs_complete"] = c.execute(
                "SELECT count(*)=0 FROM scope.dataset_building m JOIN stock.building b ON (b.dataset_id,b.building_id)=(m.source_dataset_id,m.building_id) WHERE m.dataset_id=%s AND m.selection_role='primary' AND NOT EXISTS(SELECT 1 FROM catalog.work_unit w WHERE w.owner_dataset_id=m.source_dataset_id AND w.processing_release_id=%s AND w.selection_dataset_id=m.dataset_id AND w.stage='context_linkage' AND w.partition_key=b.processing_partition AND w.state='complete')",
                (dataset_id, processing),
            ).fetchone()[0]
        return {
            "dataset": dataset_id,
            "valid": bool(layers) and all(checks.values()),
            "checks": checks,
            "layers": layers,
        }
