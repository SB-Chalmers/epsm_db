# SPDX-License-Identifier: MIT
# Copyright (c) 2026 EPSM Development contributors
# License: See LICENSE at the project root.
"""Public operation logging keeps calculation inputs and host configuration separate."""

import inspect
import logging

from epsm_db import Database
from epsm_db import stock as stock_module


def test_public_operation_accepts_explicit_context():
    parameter = inspect.signature(Database.record).parameters["log_context"]
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
            next(
                Database("postgresql://invalid")
                .stock()
                .iter_records(batch_size=0, log_context={"study_id": "logging-test", "operation_id": "parent"})
            )
    records = [record for record in caplog.records if hasattr(record, "epsm_event")]
    assert records[0].epsm_event.endswith(".started")
    assert records[-1].epsm_event.endswith(".failed")
    assert records[0].epsm_context["parent_operation_id"] == "parent"
    assert records[-1].epsm_context["study_id"] == "logging-test"
    assert records[-1].epsm_details["duration_s"] >= 0
    assert not any(record.exc_info for record in records)


def test_interleaved_stock_iterators_restore_context(monkeypatch, caplog):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from epsm_db._logging import current_context

    observed = []

    @contextmanager
    def _read():
        observed.append(current_context()["case_id"])
        yield SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchall=list)), {}

    collection = stock_module.Stock(SimpleNamespace(_read=_read))
    monkeypatch.setattr(stock_module.Stock, "_where", lambda self, connection: ("true", []))
    first = collection.iter_records(log_context={"case_id": "first"})
    second = collection.iter_records(log_context={"case_id": "second"})
    with caplog.at_level(logging.DEBUG):
        assert list(first) == []
        assert current_context() == {}
        assert list(second) == []
        assert current_context() == {}
    assert observed == ["first", "second"]
