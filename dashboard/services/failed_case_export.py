"""CSV export of the filtered Failed cases (bounded, read in keyset pages).

The export is read one page at a time (``failed_case_csv_page``), newest
failure first, so the streaming response never holds more than one page of
rows: under ASGI a sync iterator would be drained into memory before the
first byte is sent, so ``stream_failed_case_csv`` drives these sync pages
from an async iterator instead.
"""

from __future__ import annotations

import csv
import io
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime

from channels.db import database_sync_to_async
from django.db.models import Q

from auto_tester.csv_safe import neutralize_cell
from dashboard.services.failed_case_filters import (
    FailedCaseFilters,
    failed_by_label,
    failed_case_queryset,
    review_state_label,
)

# At most this many rows per export (newest failures first).
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
    "Failed at (UTC)",
    "Failed by",
    "Review",
    "Verdict",
]

_COLUMNS = (
    "test_run__project__name",
    "test_run__name",
    "test_run_id",
    "id",
    "test_case__testrail_id",
    "test_case__title",
    "finished_at",
    "failed_by",
    "checked_at",
    "testrail_source_result_id",
    "result",
)


@dataclass(frozen=True)
class ExportCursor:
    """The ``(finished_at, id)`` of the last exported row; the next page
    starts strictly after it in the export order."""

    finished_at: datetime | None
    pivot_id: int


@dataclass(frozen=True)
class ExportPage:
    """One page of CSV lines; ``cursor`` is None when the export is done."""

    text: str
    rows: int
    cursor: ExportCursor | None


def _after(cursor: ExportCursor) -> Q:
    """Rows after ``cursor`` in ``finished_at DESC, id DESC`` (Postgres puts
    NULLs first in a descending order, as the ``(status, -finished_at)``
    index does)."""
    if cursor.finished_at is None:
        return Q(finished_at__isnull=True, id__lt=cursor.pivot_id) | Q(
            finished_at__isnull=False
        )
    return Q(finished_at__lt=cursor.finished_at) | Q(
        finished_at=cursor.finished_at, id__lt=cursor.pivot_id
    )


def _csv_text(rows: Sequence[Sequence[str]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerows(rows)
    return buffer.getvalue()


def failed_case_csv_header() -> str:
    """The byte order mark and the header line."""
    return _BOM + _csv_text([EXPORT_HEADER])


def failed_case_csv_page(
    project_ids: Sequence[int],
    filters: FailedCaseFilters,
    *,
    after: ExportCursor | None,
    limit: int,
) -> ExportPage:
    """Up to ``limit`` Failed cases after ``after`` (newest failure first) as
    CSV lines, in one query. Every cell is neutralised against spreadsheet
    formula injection. The full verdict is included; logs and checkpoints
    never are. The caller bounds the total (``EXPORT_ROW_LIMIT``)."""
    queryset = failed_case_queryset(project_ids, filters)
    if after is not None:
        queryset = queryset.filter(_after(after))
    rows = list(queryset.order_by("-finished_at", "-id").values_list(*_COLUMNS)[:limit])
    lines: list[list[str]] = []
    for (
        project_name,
        run_name,
        run_id,
        pivot_id,
        testrail_id,
        title,
        finished_at,
        failed_by,
        checked_at,
        source_result_id,
        result,
    ) in rows:
        cells = [
            project_name,
            run_name or f"Run #{run_id}",
            str(run_id),
            str(pivot_id),
            testrail_id,
            title,
            finished_at.isoformat() if finished_at is not None else "",
            failed_by_label(failed_by),
            review_state_label(checked_at, source_result_id),
            result,
        ]
        lines.append([neutralize_cell(cell) for cell in cells])
    cursor: ExportCursor | None = None
    if len(rows) == limit and rows:
        last = rows[-1]
        cursor = ExportCursor(finished_at=last[6], pivot_id=last[3])
    return ExportPage(text=_csv_text(lines), rows=len(rows), cursor=cursor)


async def stream_failed_case_csv(
    project_ids: list[int], filters: FailedCaseFilters
) -> AsyncIterator[str]:
    """The whole export as CSV chunks: one bounded query per chunk in a
    worker thread, at most ``EXPORT_ROW_LIMIT`` rows, so under ASGI the
    response streams instead of being drained into memory first."""
    read_page = database_sync_to_async(failed_case_csv_page)
    yield failed_case_csv_header()
    cursor: ExportCursor | None = None
    remaining = EXPORT_ROW_LIMIT
    while remaining > 0:
        chunk = await read_page(
            project_ids,
            filters,
            after=cursor,
            limit=min(EXPORT_PAGE_SIZE, remaining),
        )
        if chunk.text:
            yield chunk.text
        remaining -= chunk.rows
        cursor = chunk.cursor
        if cursor is None:
            break
