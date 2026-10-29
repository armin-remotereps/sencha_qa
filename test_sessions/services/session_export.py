"""CSV export of one test session's cases (bounded, read in keyset pages).

Mirrors ``dashboard.services.failed_case_export``: the export is read one
page at a time (``session_csv_page``), in the order the cases were first acted
on, so the streaming response never holds more than one page; under ASGI a
sync iterator would be drained into memory before the first byte is sent, so
``stream_session_csv`` drives these sync pages from an async iterator. The
caller has already checked that the session belongs to the user.
"""

from __future__ import annotations

import csv
import io
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypedDict, cast

from channels.db import database_sync_to_async

from auto_tester.csv_safe import neutralize_cell
from auto_tester.durations import format_duration
from test_sessions.models import TestSessionCase
from test_sessions.services.case_outcome import (
    BLOCKED_REASON,
    DURATION,
    FAILED_BY,
    FINISHED_AT,
    RESULT,
    STARTED_AT,
    STATUS,
    actions_label,
    blocked_reason_label,
    failed_by_label,
    outcome_annotations,
    performed_by_label,
    status_label,
)
from test_sessions.services.session_queries import run_label

# At most this many rows per export.
EXPORT_ROW_LIMIT = 50_000
# Rows per database round trip (and per streamed chunk).
EXPORT_PAGE_SIZE = 1000

# A UTF-8 byte order mark, so spreadsheets detect the encoding.
_BOM = "﻿"

EXPORT_HEADER = [
    "Project",
    "Run",
    "Run ID",
    "Run case ID",
    "TestRail ID",
    "Case",
    "Started (UTC)",
    "Finished (UTC)",
    "Duration",
    "Duration (seconds)",
    "Result",
    "Failed by / Blocked reason",
    "Performed by",
    "Your actions",
    "Your person steps",
    "First action (UTC)",
    "Last action (UTC)",
    "Snapshot",
    "Verdict",
]

_COLUMNS = (
    "id",
    "project_name",
    "run_name",
    "run_ref",
    "pivot_ref",
    "testrail_case_id",
    "case_title",
    "run_help_mode",
    "first_activity",
    "first_activity_at",
    "last_activity",
    "last_activity_at",
    "person_step_count",
    "frozen_at",
    STATUS,
    FAILED_BY,
    BLOCKED_REASON,
    STARTED_AT,
    FINISHED_AT,
    DURATION,
    RESULT,
)


class _ExportValues(TypedDict):
    id: int
    project_name: str
    run_name: str
    run_ref: int | None
    pivot_ref: int | None
    testrail_case_id: str
    case_title: str
    run_help_mode: str
    first_activity: str
    first_activity_at: datetime
    last_activity: str
    last_activity_at: datetime
    person_step_count: int
    frozen_at: datetime | None
    outcome_status: str | None
    outcome_failed_by: str | None
    outcome_blocked_reason: str | None
    outcome_started_at: datetime | None
    outcome_finished_at: datetime | None
    outcome_duration: timedelta | None
    outcome_result: str | None


@dataclass(frozen=True)
class ExportPage:
    """One page of CSV lines; ``after_id`` is None when the export is done."""

    text: str
    rows: int
    after_id: int | None


def _csv_text(rows: Sequence[Sequence[str]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerows(rows)
    return buffer.getvalue()


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _int_text(value: int | None) -> str:
    return str(value) if value is not None else ""


def _cells(row: _ExportValues) -> list[str]:
    status = row["outcome_status"] or ""
    duration = row["outcome_duration"]
    frozen_at = row["frozen_at"]
    reason = failed_by_label(status, row["outcome_failed_by"]) or (
        blocked_reason_label(status, row["outcome_blocked_reason"])
    )
    return [
        row["project_name"],
        run_label(row["run_name"], row["run_ref"]),
        _int_text(row["run_ref"]),
        _int_text(row["pivot_ref"]),
        row["testrail_case_id"],
        row["case_title"],
        _iso(row["outcome_started_at"]),
        _iso(row["outcome_finished_at"]),
        format_duration(duration) if duration is not None else "",
        str(int(duration.total_seconds())) if duration is not None else "",
        status_label(status),
        reason,
        performed_by_label(row["run_help_mode"]),
        actions_label(row["first_activity"], row["last_activity"]),
        str(row["person_step_count"]),
        _iso(row["first_activity_at"]),
        _iso(row["last_activity_at"]),
        "Live" if frozen_at is None else f"As of {frozen_at.isoformat()}",
        row["outcome_result"] or "",
    ]


def session_csv_header() -> str:
    """The byte order mark and the header line."""
    return _BOM + _csv_text([EXPORT_HEADER])


def session_csv_page(
    session_id: int, *, after_id: int | None, limit: int
) -> ExportPage:
    """Up to ``limit`` cases of ``session_id`` after row ``after_id`` (id
    ascending) as CSV lines, in one query. Every cell is neutralised against
    spreadsheet formula injection. The verdict is included; logs, checkpoints
    and screenshots never are. The caller bounds the total."""
    queryset = TestSessionCase.objects.filter(session_id=session_id)
    if after_id is not None:
        queryset = queryset.filter(id__gt=after_id)
    rows = cast(
        list[_ExportValues],
        list(
            queryset.annotate(**outcome_annotations(with_result=True))
            .order_by("id")
            .values(*_COLUMNS)[:limit]
        ),
    )
    lines = [[neutralize_cell(cell) for cell in _cells(row)] for row in rows]
    next_after = rows[-1]["id"] if rows and len(rows) == limit else None
    return ExportPage(text=_csv_text(lines), rows=len(rows), after_id=next_after)


async def stream_session_csv(session_id: int) -> AsyncIterator[str]:
    """The whole export as CSV chunks: one bounded query per chunk in a
    worker thread, at most ``EXPORT_ROW_LIMIT`` rows, so under ASGI the
    response streams instead of being drained into memory first."""
    read_page = database_sync_to_async(session_csv_page)
    yield session_csv_header()
    after_id: int | None = None
    remaining = EXPORT_ROW_LIMIT
    while remaining > 0:
        chunk = await read_page(
            session_id, after_id=after_id, limit=min(EXPORT_PAGE_SIZE, remaining)
        )
        if chunk.text:
            yield chunk.text
        remaining -= chunk.rows
        after_id = chunk.after_id
        if after_id is None:
            break
