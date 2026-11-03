"""CSV export of a run's case analysis, in report order.

Built from the report read (a fixed number of queries; the analysable cases
of a run are capped by ``RUN_TRIAGE_MAX_CASES``, no logs or checkpoints
loaded) and yielded in chunks. Every cell goes through ``neutralize_cell``.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator, Sequence
from typing import Final

from django.conf import settings

from auto_tester.csv_safe import neutralize_cell
from projects.models import Project, TestRun
from run_triage.services.report import get_triage_report
from run_triage.types import ReportCase, TriageReport

# A UTF-8 byte order mark, so spreadsheets detect the encoding.
_BOM: Final = "﻿"
# Rows per yielded chunk.
_CHUNK_ROWS: Final = 200

EXPORT_HEADER: Final = [
    "TestRail test ID",
    "Link",
    "Case",
    "Run case status",
    "Category",
    "Severity",
    "Group finding",
    "Sub-group",
    "Case finding",
    "Untested",
    "Out of date",
    "Corrected by",
]


def triage_csv_filename(test_run_id: int) -> str:
    return f"run-{test_run_id}-case-analysis.csv"


def triage_csv_rows(test_run: TestRun, project: Project) -> Iterator[str]:
    """The byte order mark and header, then the rows in chunks."""
    yield _BOM + _csv_text([EXPORT_HEADER])
    chunk: list[list[str]] = []
    for row in _rows(get_triage_report(test_run, project)):
        chunk.append([neutralize_cell(cell) for cell in row])
        if len(chunk) >= _CHUNK_ROWS:
            yield _csv_text(chunk)
            chunk = []
    if chunk:
        yield _csv_text(chunk)


def _rows(report: TriageReport) -> Iterator[list[str]]:
    for category in report.categories:
        for group in category.groups:
            for case in group.cases:
                yield _cells(case, category.label, group.title, "")
            for sub in group.sub_groups:
                for case in sub.cases:
                    yield _cells(case, category.label, group.title, sub.title)
        for case in category.ungrouped:
            yield _cells(case, category.label, "", "")
    for case in report.unclassified:
        yield _cells(case, "", "", "")


def _cells(case: ReportCase, category: str, group: str, sub_group: str) -> list[str]:
    return [
        case.ref[1:] if case.ref.startswith("T") else "",
        _absolute(case.link_url),
        case.title,
        case.status_label,
        category or "Not classified",
        case.severity_label,
        group,
        sub_group,
        case.finding,
        "yes" if case.untested else "no",
        "yes" if case.stale else "no",
        case.corrected_by_label,
    ]


def _absolute(url: str) -> str:
    if url.startswith("/") and settings.SITE_URL:
        return settings.SITE_URL.rstrip("/") + url
    return url


def _csv_text(rows: Sequence[Sequence[str]]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer).writerows(rows)
    return buffer.getvalue()
