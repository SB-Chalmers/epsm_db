"""Informational progress; committed work checkpoints remain authoritative."""
from .database import _connect
from contextlib import nullcontext


def report(url, source, layer, step, *, state="running", expected_rows=None, error=None):
    from .jobs import _worker
    worker = _worker.get()
    if worker and worker["url"] == url:
        if "progress_connection" not in worker:
            worker["progress_connection"] = _connect(url)
            worker["progress_connection"].autocommit = True
        context = nullcontext(worker["progress_connection"])
    else:
        context = _connect(url)
    with context as c:
        c.execute(
            "INSERT INTO catalog.ingestion_progress(source_dataset_id,layer,step,state,expected_rows,error) "
            "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(source_dataset_id,layer,step) DO UPDATE SET "
            "state=EXCLUDED.state,expected_rows=COALESCE(EXCLUDED.expected_rows,ingestion_progress.expected_rows),"
            "error=EXCLUDED.error,updated_at=clock_timestamp()",
            (source, layer, step, state, expected_rows, error))
