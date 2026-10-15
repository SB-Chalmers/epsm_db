"""Informational progress; committed work checkpoints remain authoritative."""
from .database import _connect
from contextlib import nullcontext
from threading import Lock
from time import monotonic

from ._logging import context_logger

logger = context_logger(__name__)
_last_log = {}
_log_lock = Lock()


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
    # Database progress remains current even when terminal updates are throttled.
    now = monotonic()
    with _log_lock:
        key = (source, layer)
        emit = state in {"complete", "failed"} or now - _last_log.get(key, float("-inf")) >= 10
        if emit:
            _last_log[key] = now
    if emit:
        logger.log(40 if state == "failed" else 20, "Ingestion %s: %s", layer, state,
                   extra={"epsm_event": "database.ingestion.progress",
                          "epsm_details": {"source_id": source, "layer": layer,
                                           "state": state, "expected_rows": expected_rows}})
