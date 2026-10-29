"""Bounded COPY writes and transaction-fenced, resumable database work."""

from contextlib import contextmanager
from contextvars import ContextVar
import json
from uuid import uuid4

from psycopg import sql
from psycopg.types.json import Jsonb

from .database import _connect, application_digest
from .hashing import content_hash
from .version import VERSION

_worker = ContextVar("ingestion_worker", default=None)


@contextmanager
def serial_worker(url):
    """Reuse transport resources while committing each work unit separately."""
    with _connect(url) as connection:
        token = _worker.set(
            {
                "url": url,
                "connection": connection,
                "types": {},
                "stages": {},
                "digest": application_digest(),
                "active": False,
            }
        )
        try:
            yield
        finally:
            resources = _worker.get()
            if resources and resources.get("progress_connection"):
                resources["progress_connection"].close()
            _worker.reset(token)


@contextmanager
def work_connection(url):
    worker = _worker.get()
    if worker is None or worker["url"] != url or worker["active"]:
        with _connect(url) as connection:
            yield connection
        return
    connection = worker["connection"]
    worker["active"] = True
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        worker["stages"].clear()
        raise
    finally:
        worker["active"] = False


def execution_digest():
    worker = _worker.get()
    return worker["digest"] if worker else application_digest()


class BatchTooLarge(ValueError):
    pass


def serialized_size(value):
    """Conservative COPY text/JSON byte accounting, including geometry and bytea."""
    def encode(item):
        if hasattr(item, "wkb"):
            return bytes(item.wkb).hex()
        if isinstance(item, (bytes, bytearray, memoryview)):
            return bytes(item).hex()
        return str(item)
    return len(json.dumps(value, ensure_ascii=False, default=encode,
                          separators=(",", ":")).encode("utf-8"))


def bulk_insert(c, table, rows, *, conflict="nothing"):
    if not rows:
        return 0
    from .compact import project_write
    table, rows, conflict = project_write(c, table, rows, conflict)
    if table is None:
        return conflict
    if not rows:
        return 0
    schema, name = table.split(".")
    columns = list(rows[0])
    target = sql.Identifier(schema, name)
    names = sql.SQL(",").join(map(sql.Identifier, columns))
    worker = _worker.get()
    if worker and worker["connection"] is c and "batch_bytes" in worker:
        worker["batch_bytes"] += serialized_size(rows)
        if worker["batch_bytes"] > worker["max_batch_bytes"]:
            raise BatchTooLarge("Serialized output exceeds the bounded source batch")
    cache = worker["types"] if worker and worker["connection"] is c else {}
    types = cache.get(table)
    if types is None:
        types = dict(
            c.execute(
                "SELECT a.attname,t.typname FROM pg_attribute a JOIN pg_type t ON t.oid=a.atttypid WHERE a.attrelid=%s::regclass AND a.attnum>0",
                (table,),
            )
        )
        cache[table] = types
    geometry = {k for k in columns if types[k] == "geometry"}
    direct = conflict == "append" and not geometry
    reusable = worker and worker["connection"] is c
    stage_key = (table, tuple(columns), tuple(sorted(geometry)))
    if direct:
        destination = target
    else:
        stage_name = "stage_" + content_hash(stage_key)[:24]
        stage = sql.Identifier(stage_name)
        stages = worker["stages"] if reusable else {}
        if stage_key not in stages:
            c.execute(sql.SQL("DROP TABLE IF EXISTS pg_temp.{}").format(stage))
            c.execute(sql.SQL("CREATE TEMP TABLE {} ON COMMIT {} AS SELECT {} FROM {} WITH NO DATA").format(
                stage, sql.SQL("PRESERVE ROWS" if reusable else "DROP"), names, target))
            for key in geometry:
                c.execute(sql.SQL("ALTER TABLE {} ALTER COLUMN {} TYPE bytea USING NULL::bytea").format(
                    stage, sql.Identifier(key)))
            stages[stage_key] = stage_name
        else:
            c.execute(sql.SQL("TRUNCATE {}").format(stage))
        destination = stage
    with c.cursor().copy(sql.SQL("COPY {} ({}) FROM STDIN").format(destination, names)) as copy:
        for row in rows:
            values = []
            for key in columns:
                value = row[key]
                if value is not None and key in geometry:
                    value = bytes(value.wkb) if hasattr(value, "wkb") else bytes(value)
                if value is not None and types[key] == "jsonb":
                    value = Jsonb(value)
                values.append(value)
            copy.write_row(values)
    if direct:
        return len(rows)
    expressions = sql.SQL(",").join(
        sql.SQL("ST_GeomFromWKB({},3006)").format(sql.Identifier(k))
        if k in geometry
        else sql.Identifier(k)
        for k in columns
    )
    if isinstance(conflict, tuple):
        updates = sql.SQL(",").join(
            sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(k), sql.Identifier(k))
            for k in columns
            if k not in conflict
        )
        changed = [k for k in columns if k not in conflict]
        if changed:
            clause = sql.SQL(" ON CONFLICT ({}) DO UPDATE SET {} WHERE ROW({}) IS DISTINCT FROM ROW({})").format(
                sql.SQL(",").join(map(sql.Identifier, conflict)), updates,
                sql.SQL(",").join(sql.SQL("target.{}").format(sql.Identifier(k)) for k in changed),
                sql.SQL(",").join(sql.SQL("EXCLUDED.{}").format(sql.Identifier(k)) for k in changed))
        else:
            clause = sql.SQL(" ON CONFLICT DO NOTHING")
        if table=="stock.property_designation" and "representative_position" in columns:
            clause=sql.SQL(" ON CONFLICT ({}) DO UPDATE SET {} WHERE "
                "EXCLUDED.representative_position/1024<target.representative_position/1024 OR "
                "(EXCLUDED.representative_position/1024=target.representative_position/1024 AND "
                "EXCLUDED.representative_position>target.representative_position)").format(
                    sql.SQL(",").join(map(sql.Identifier,conflict)),updates)
    else:
        clause = sql.SQL(" ON CONFLICT DO NOTHING") if conflict == "nothing" else sql.SQL("")
    result = c.execute(
        sql.SQL("INSERT INTO {} AS target ({}) SELECT {} FROM {}").format(target, names, expressions, stage)
        + clause
    )
    if not reusable:
        c.execute(sql.SQL("DROP TABLE {}").format(stage))
    return result.rowcount


@contextmanager
def work(url, source, stage, partition, signature, *, processing=None, selection=None):
    """Hold a transaction advisory lock; output and completion commit together.

    The lock is the fencing mechanism. A disconnected worker loses its transaction
    and lock; another claimant increments the generation only after acquiring it.
    """
    key = content_hash([source, processing, selection, stage, partition])
    with work_connection(url) as c:
        c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (key,))
        row = c.execute(
            "SELECT work_id,input_signature,state,generation FROM catalog.work_unit WHERE owner_dataset_id=%s AND processing_release_id IS NOT DISTINCT FROM %s AND selection_dataset_id IS NOT DISTINCT FROM %s AND stage=%s AND partition_key=%s FOR UPDATE",
            (source, processing, selection, stage, partition),
        ).fetchone()
        if row and row[1] != signature:
            raise ValueError("Work input signature changed; use a new immutable release")
        if row and row[2] == "complete":
            yield None
            return
        run = uuid4()
        c.execute(
            "INSERT INTO catalog.process_run(run_id,dataset_id,layer,processing_release_id,policy_version,code_version,status,input_versions) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                run,
                source,
                stage,
                processing,
                "bounded-work-v1",
                f"sweden-ubem-{VERSION}:{execution_digest()}",
                "running",
                Jsonb({"signature": signature, "partition": partition, "selection": selection}),
            ),
        )
        if row:
            work_id, generation = row[0], row[3] + 1
            c.execute(
                "UPDATE catalog.work_unit SET state='running',generation=%s,run_id=%s,error=NULL WHERE work_id=%s",
                (generation, run, work_id),
            )
        else:
            work_id = c.execute(
                "INSERT INTO catalog.work_unit(owner_dataset_id,processing_release_id,selection_dataset_id,stage,partition_key,input_signature,state,generation,run_id) VALUES(%s,%s,%s,%s,%s,%s,'running',1,%s) RETURNING work_id",
                (source, processing, selection, stage, partition, signature, run),
            ).fetchone()[0]
            generation = 1
        state = {"connection": c, "run_id": run, "rows": 0, "checkpoint": {}}
        try:
            yield state
            changed = c.execute(
                "UPDATE catalog.work_unit SET state='complete',rows_written=%s,checkpoint=%s WHERE work_id=%s AND generation=%s AND state='running'",
                (state["rows"], Jsonb(state["checkpoint"]), work_id, generation),
            ).rowcount
            if changed != 1:
                raise RuntimeError("Worker generation was superseded")
            c.execute(
                "UPDATE catalog.process_run SET status='complete',finished_at=clock_timestamp() WHERE run_id=%s",
                (run,),
            )
        except BaseException as error:
            # Transaction rollback includes the claim, data, run, and checkpoint.
            # A failed attempt cannot leave running work or partial output behind.
            c.rollback()
            worker = _worker.get()
            if worker and worker["connection"] is c:
                worker["stages"].clear()
            # Record the failed attempt only after its output transaction rolls
            # back. Never overwrite another worker's completion or claim.
            try:
                with _connect(url) as audit:
                    audit.execute(
                        "INSERT INTO catalog.process_run(run_id,dataset_id,layer,processing_release_id,policy_version,code_version,status,input_versions,finished_at,error) VALUES(%s,%s,%s,%s,%s,%s,'failed',%s,clock_timestamp(),%s)",
                        (
                            run,
                            source,
                            stage,
                            processing,
                            "bounded-work-v1",
                            f"sweden-ubem-{VERSION}:{execution_digest()}",
                            Jsonb(
                                {
                                    "signature": signature,
                                    "partition": partition,
                                    "selection": selection,
                                }
                            ),
                            str(error)[:4000],
                        ),
                    )
                    audit.execute(
                        "INSERT INTO catalog.work_unit(owner_dataset_id,processing_release_id,selection_dataset_id,stage,partition_key,input_signature,state,generation,run_id,error) VALUES(%s,%s,%s,%s,%s,%s,'failed',%s,%s,%s) ON CONFLICT DO NOTHING",
                        (
                            source,
                            processing,
                            selection,
                            stage,
                            partition,
                            signature,
                            generation,
                            run,
                            str(error)[:4000],
                        ),
                    )
            except Exception:
                # Preserve the original failure if the server itself is down.
                pass
            raise


def rows(c, statement, params=()):
    cursor = c.execute(statement, params)
    return [dict(zip([d.name for d in cursor.description], r)) for r in cursor]


def batches(values, size=8192, max_bytes=64 * 1024**2):
    buffer = []
    byte_size = 0
    for value in values:
        value_bytes = serialized_size(value)
        if value_bytes > max_bytes:
            locator = next((value[k] for k in ("_source_position", "source_record", "_fid") if isinstance(value,dict) and k in value), None)
            raise BatchTooLarge(f"Single source record exceeds serialized byte limit; locator={locator}")
        if buffer and byte_size + value_bytes > max_bytes:
            yield buffer
            buffer = []
            byte_size = 0
        buffer.append(value)
        byte_size += value_bytes
        if len(buffer) == size:
            yield buffer
            buffer = []
            byte_size = 0
    if buffer:
        yield buffer
