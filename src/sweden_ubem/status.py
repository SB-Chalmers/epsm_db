"""Read-only progress and storage inspection without scanning national source rows."""

from datetime import datetime, timezone

from .database import _connect


def database_status(url):
    with _connect(url) as c:
        c.execute("SET TRANSACTION READ ONLY")
        sources = [
            dict(dataset=dataset, sealed=sealed)
            for dataset, sealed in c.execute(
                "SELECT dataset_id,sealed FROM catalog.dataset "
                "WHERE dataset_kind='national_source' ORDER BY dataset_id"
            )
        ]
        selections = [
            dict(dataset=dataset, kind=kind, municipality=municipality, sealed=sealed)
            for dataset, kind, municipality, sealed in c.execute(
                "SELECT dataset_id,dataset_kind,municipality_code,sealed FROM catalog.dataset "
                "WHERE dataset_kind<>'national_source' ORDER BY dataset_id"
            )
        ]
        layers = [
            dict(dataset=dataset, layer=layer, status=state)
            for dataset, layer, state in c.execute(
                "SELECT dataset_id,layer,loading_status FROM catalog.dataset_layer "
                "ORDER BY dataset_id,layer"
            )
        ]
        work = [
            dict(
                dataset=dataset,
                stage=stage,
                step=step,
                state=state,
                committed_batches=batches,
                source_rows=source_rows,
                output_rows=output_rows,
                last_commit=last_commit,
            )
            for dataset, stage, step, state, batches, source_rows, output_rows, last_commit in c.execute(
                "SELECT w.owner_dataset_id,w.stage,"
                "regexp_replace(w.partition_key,':([0-9]+|EOF)$',''),w.state,"
                "count(*) FILTER(WHERE NOT COALESCE((w.checkpoint->>'file_complete')::boolean,false)),"
                "sum((w.checkpoint->>'source_rows')::bigint) FILTER(WHERE w.state='complete')::bigint,"
                "sum(w.rows_written)::bigint,"
                "max(r.finished_at) FROM catalog.work_unit w "
                "LEFT JOIN catalog.process_run r USING(run_id) "
                "GROUP BY 1,2,3,4 ORDER BY 1,2,3,4"
            )
        ]
        storage = [
            dict(table=table, heap_and_toast_bytes=heap, index_bytes=index)
            for table, heap, index in c.execute(
                "SELECT n.nspname||'.'||r.relname,pg_table_size(r.oid),"
                "pg_indexes_size(r.oid) FROM pg_class r "
                "JOIN pg_namespace n ON n.oid=r.relnamespace "
                "WHERE r.relkind='r' AND n.nspname=ANY(%s) "
                "ORDER BY pg_table_size(r.oid)+pg_indexes_size(r.oid) DESC LIMIT 15",
                (
                    [
                        "catalog",
                        "raw_lm",
                        "raw_height",
                        "raw_epc",
                        "raw_scb",
                        "stock",
                        "evidence",
                        "context",
                        "scope",
                        "quality",
                    ],
                ),
            )
        ]
        activity = [
            dict(state=state, wait_type=wait_type, wait_event=wait, statement_seconds=seconds)
            for state, wait_type, wait, seconds in c.execute(
                "SELECT state,wait_event_type,wait_event,"
                "extract(epoch FROM clock_timestamp()-query_start)::double precision "
                "FROM pg_stat_activity WHERE datname=current_database() "
                "AND pid<>pg_backend_pid() AND state='active'"
            )
        ]
        size = c.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        name = c.execute("SELECT current_database()").fetchone()[0]
        index_bytes = c.execute("SELECT COALESCE(sum(pg_indexes_size(r.oid)),0)::bigint FROM pg_class r JOIN pg_namespace n ON n.oid=r.relnamespace WHERE r.relkind='r' AND n.nspname IN ('catalog','raw_lm','raw_height','raw_epc','raw_scb','stock','evidence','context','scope','quality')").fetchone()[0]
        progress = []
        for source, layer, step, state, expected, error, started, updated in c.execute(
            "SELECT source_dataset_id,layer,step,state,expected_rows,error,started_at,updated_at "
            "FROM catalog.ingestion_progress ORDER BY source_dataset_id,layer,started_at"):
            committed, outputs, last_commit, files = c.execute(
                "SELECT COALESCE(sum((checkpoint->>'source_rows')::bigint) FILTER(WHERE w.state='complete'),0)::bigint,"
                "COALESCE(sum(rows_written) FILTER(WHERE w.state='complete'),0)::bigint,max(r.finished_at),"
                "count(*) FILTER(WHERE w.state='complete' AND checkpoint->>'file_complete'='true') "
                "FROM catalog.work_unit w LEFT JOIN catalog.process_run r USING(run_id) "
                "WHERE w.owner_dataset_id=%s AND w.stage=%s AND regexp_replace(w.partition_key,':([0-9]+|EOF)$','')=%s",
                (source, "import:" + layer, step)).fetchone()
            elapsed = max(0.0, ((updated if state != "running" else datetime.now(timezone.utc)) - started).total_seconds())
            throughput = committed / elapsed if elapsed > 0 and committed else None
            eta = (expected - committed) / throughput if state == "running" and expected and throughput and committed >= 8192 else None
            progress.append(dict(dataset=source,layer=layer,current_substep=step,state=state,
                                 expected_source_rows=expected,committed_source_rows=committed,
                                 output_rows=outputs,committed_files=files,elapsed_seconds=elapsed,
                                 rows_per_second=throughput,eta_seconds=eta,last_commit=last_commit,error=error))
        file_progress = [dict(layer=layer,expected_files=expected,committed_files=committed)
                         for layer, expected, committed in c.execute(
            "SELECT v.layer,count(*),count(*) FILTER(WHERE f.ready OR a.asset_key IS NOT NULL) FROM catalog.source_file_data f "
            "JOIN catalog.source_version v USING(source_version_id) "
            "LEFT JOIN catalog.asset_verification a ON a.asset_key=f.asset_key AND v.layer='heights' "
            "GROUP BY v.layer ORDER BY v.layer")]
        height_measurements=[dict(source_version=version,expected_file_units=expected,
                                 committed_file_units=committed,verified_physical_assets=physical,
                                 committed_measurement_rows=rows)
            for version,expected,committed,physical,rows in c.execute(
                "SELECT m.source_version_id,count(*),count(*) FILTER(WHERE a.asset_key IS NOT NULL),"
                "count(DISTINCT a.asset_key),"
                "(SELECT COALESCE(sum(v.source_rows),0)::bigint FROM catalog.asset_verification v "
                "WHERE v.asset_key IN (SELECT f2.asset_key FROM evidence.measurement_source m2 "
                "JOIN catalog.source_file_data f2 USING(occurrence_key) WHERE m2.source_version_id=m.source_version_id)) "
                "FROM evidence.measurement_source m JOIN catalog.source_file_data f USING(occurrence_key) "
                "LEFT JOIN catalog.asset_verification a USING(asset_key) GROUP BY m.source_version_id")]
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "database": name,
        "database_bytes": size,
        "sources": sources,
        "selections": selections,
        "layers": layers,
        "committed_work": work,
        "progress": progress,
        "source_files": file_progress,
        "height_measurements": height_measurements,
        "database_index_bytes": index_bytes,
        "largest_tables": storage,
        "active_statements": activity,
        "progress_note": (
            "Source import and municipal/national model processing are separate. "
            "A source layer remains not_loaded until all its steps and checks finish. "
            "Committed batches are resumable; the current transaction becomes visible at commit. "
            "Database bytes measure disk storage, not RAM."
        ),
    }
