# SPDX-License-Identifier: MIT
# Copyright (c) 2026 EPSM Development contributors
# License: See LICENSE at the project root.
"""Public operation logging keeps calculation inputs and host configuration separate."""

import inspect
import logging

from sweden_ubem.io import RecordFetcher


def test_public_operation_accepts_explicit_context():
    parameter = inspect.signature(RecordFetcher.from_uuid).parameters["log_context"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is None


def test_import_does_not_install_output_handlers():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import epsm_db; import logging; assert not any(isinstance(h, logging.StreamHandler) for h in logging.getLogger('epsm_db').handlers)",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stdout and not result.stderr


def test_rejected_operation_keeps_context_and_reports_failure(caplog):
    import pytest

    with caplog.at_level(logging.DEBUG):
        with pytest.raises((ValueError, TypeError, AttributeError)):
            RecordFetcher().from_uuid(
                "invalid-uuid", log_context={"study_id": "logging-test", "operation_id": "parent"}
            )
    records = [record for record in caplog.records if hasattr(record, "epsm_event")]
    assert records[0].epsm_event.endswith(".started")
    assert records[-1].epsm_event.endswith(".failed")
    assert records[0].epsm_context["parent_operation_id"] == "parent"
    assert records[-1].epsm_context["study_id"] == "logging-test"
    assert records[-1].epsm_details["duration_s"] >= 0
    assert not any(record.exc_info for record in records)


def test_interleaved_stock_iterators_restore_context(monkeypatch, caplog):
    from types import SimpleNamespace

    from sweden_ubem import stock
    from sweden_ubem._logging import current_context

    observed = []

    class Page(list):
        receipt = SimpleNamespace(truncated=False)

    class Reader:
        def all(self, **options):
            observed.append(current_context()["case_id"])
            return Page(["record"])

    monkeypatch.setattr(stock.fetch_records, "using", lambda *args, **kwargs: Reader())
    collection = stock.Stock(SimpleNamespace(database_url="unused", dataset_id="test"))
    first = collection.iter_records(log_context={"case_id": "first"})
    second = collection.iter_records(log_context={"case_id": "second"})
    with caplog.at_level(logging.DEBUG):
        assert next(first) == "record"
        assert current_context() == {}
        assert next(second) == "record"
        assert current_context() == {}
        first.close()
        assert list(second) == []
    assert observed == ["first", "second"]
    events = [r.epsm_event for r in caplog.records if hasattr(r, "epsm_event")]
    assert "database.stock.read.cancelled" in events
    assert events.count("database.stock.read.completed") == 1
