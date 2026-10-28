"""CSV export of the filtered Failed cases (streamed, bounded)."""

from __future__ import annotations

import csv
from collections.abc import Iterator, Sequence

from auto_tester.csv_safe import neutralize_cell
from dashboard.services.failed_case_filters import (
    FailedCaseFilters,
    failed_by_label,
    failed_case_queryset,
    review_state_label,
)

# At most this many rows per export (newest failures first).
EXPORT_ROW_LIMIT = 50_000
_CHUNK_SIZE = 1000

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


def iter_failed_case_rows(
    project_ids: Sequence[int], filters: FailedCaseFilters
) -> Iterator[list[str]]:
    """The header, then one row per Failed case (newest first, at most
    ``EXPORT_ROW_LIMIT``), read in chunks with a server-side cursor. Every
    cell is neutralised against spreadsheet formula injection. The full
    verdict is included; logs and checkpoints never are."""
    yield list(EXPORT_HEADER)
    rows = (
        failed_case_queryset(project_ids, filters)
        .order_by("-finished_at", "-id")
        .values_list(
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
        )[:EXPORT_ROW_LIMIT]
    )
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
    ) in rows.iterator(chunk_size=_CHUNK_SIZE):
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
        yield [neutralize_cell(cell) for cell in cells]


class _Echo:
    """A write-only file object: ``csv.writer`` returns each formatted line."""

    def write(self, value: str) -> str:
        return value


def iter_failed_case_csv(
    project_ids: Sequence[int], filters: FailedCaseFilters
) -> Iterator[str]:
    """``iter_failed_case_rows`` formatted as CSV lines, for streaming,
    after a UTF-8 byte order mark so spreadsheets detect the encoding."""
    yield "\ufeff"
    writer = csv.writer(_Echo())
    for row in iter_failed_case_rows(project_ids, filters):
        yield writer.writerow(row)
