# SPDX-License-Identifier: MIT
# Copyright (c) 2026 EPSM Development contributors
# License: See LICENSE at the project root.
"""Progress logs stay bounded without reducing durable database updates."""

import logging
from contextlib import nullcontext
from types import SimpleNamespace

from sweden_ubem import progress


def test_progress_throttles_console_updates_and_always_reports_completion(monkeypatch, caplog):
    updates = []
    connection = SimpleNamespace(execute=lambda *args: updates.append(args))
    monkeypatch.setattr(progress, "_connect", lambda url: nullcontext(connection))
    monkeypatch.setattr(progress, "_last_log", {})
    ticks = iter((0, 1, 11, 11))
    monkeypatch.setattr(progress, "monotonic", lambda: next(ticks))
    with caplog.at_level(logging.INFO):
        for state in ("running", "running", "running", "complete"):
            progress.report("unused-test-url", "source", "geometry", "step", state=state)
    records = [
        r for r in caplog.records if getattr(r, "epsm_event", "") == "database.ingestion.progress"
    ]
    assert len(updates) == 4 and len(records) == 3
    assert records[-1].epsm_details["state"] == "complete"
