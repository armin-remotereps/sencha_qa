"""Shared reads of a run's case analysis state (pivots joined with rows,
groups, notes, the analysis thread) for the read-side modules.

A row is out of date when its case's status or finish time differs from the
values stored when it was classified; an analysable case with no row is
unclassified. Rows of cases that are no longer analysable (a re-run in
progress, a case now Passed) stay until the next classification and read as
out of date.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, TypedDict, cast

from django.db.models import Exists, OuterRef
from django.utils import timezone

from machine_setup.models import SetupConversation, SetupConversationKind, SetupTurn
from projects.models import TestRunTestCase
from run_triage.case_queries import has_result_annotation
from run_triage.case_scope import CaseScope, case_scope
from run_triage.models import RunTriage, TriageGroup
from run_triage.services.taxonomy_text import CATEGORY_ORDER, category_label
from run_triage.taxonomy import TriageSeverity, TriageSource
from run_triage.tool_text import count_suffix
from run_triage.turn_liveness import live_turn_q
from run_triage.types import CategoryCount

_CRITICAL: Final = str(TriageSeverity.CRITICAL)
NOT_GROUPED: Final = "Not grouped"


class _JoinValues(TypedDict):
    id: int
    status: str
    finished_at: datetime | None
    has_result: bool
    testrail_source_result_id: int | None
    testrail_test_id: int | None
    testrail_source_test_id: int | None
    triage_rows__id: int | None
    triage_rows__category: str | None
    triage_rows__severity: str | None
    triage_rows__untested: bool | None
    triage_rows__group_id: int | None
    triage_rows__source: str | None
    triage_rows__classified_status: str | None
    triage_rows__classified_finished_at: datetime | None


@dataclass(frozen=True)
class RowView:
    pivot_id: int
    category: str
    severity: str
    finding: str
    untested: bool
    group_id: int | None
    source: str
    corrected_by_label: str
    stale: bool


@dataclass(frozen=True)
class RunState:
    """Counts of one run, from the pivot/row join (one query)."""

    total: int
    passed: int
    analysable: int
    not_finished: int
    unclassified: int
    rows: tuple[RowView, ...]

    @property
    def stale(self) -> int:
        return sum(row.stale for row in self.rows)

    @property
    def corrected(self) -> int:
        return sum(row.source == TriageSource.PERSON for row in self.rows)


@dataclass(frozen=True)
class GroupView:
    id: int
    category: str
    parent_id: int | None
    title: str
    source: str


# ---------------------------------------------------------------- loaders


def is_stale(
    classified_status: str | None,
    classified_finished_at: datetime | None,
    status: str,
    finished_at: datetime | None,
) -> bool:
    return classified_status != status or classified_finished_at != finished_at


def scope_of(status: str, has_result: bool) -> CaseScope:
    return case_scope(status, "x" if has_result else "")


def load_state(test_run_id: int) -> RunState:
    """Every case of the run with its row, if any (one query)."""
    values = cast(
        list[_JoinValues],
        list(
            TestRunTestCase.objects.filter(test_run_id=test_run_id)
            .annotate(has_result=has_result_annotation())
            .values(*_JoinValues.__annotations__)
        ),
    )
    scopes: Counter[CaseScope] = Counter()
    rows: list[RowView] = []
    unclassified = 0
    for value in values:
        scope = scope_of(value["status"], value["has_result"])
        scopes[scope] += 1
        category = value["triage_rows__category"]
        if scope != CaseScope.ANALYSED:
            # A row left on a case that is now Passed or not finished is
            # dropped at the next classify call; like the report, ignore it.
            continue
        if value["triage_rows__id"] is None or category is None:
            unclassified += 1
            continue
        rows.append(
            RowView(
                pivot_id=value["id"],
                category=category,
                severity=value["triage_rows__severity"] or "",
                finding="",
                untested=bool(value["triage_rows__untested"]),
                group_id=value["triage_rows__group_id"],
                source=value["triage_rows__source"] or "",
                corrected_by_label="",
                stale=is_stale(
                    value["triage_rows__classified_status"],
                    value["triage_rows__classified_finished_at"],
                    value["status"],
                    value["finished_at"],
                ),
            )
        )
    return RunState(
        total=len(values),
        passed=scopes[CaseScope.PASSED],
        analysable=scopes[CaseScope.ANALYSED],
        not_finished=scopes[CaseScope.NOT_FINISHED],
        unclassified=unclassified,
        rows=tuple(rows),
    )


def load_groups(test_run_id: int) -> list[GroupView]:
    return [
        GroupView(
            id=group_id,
            category=category,
            parent_id=parent_id,
            title=title,
            source=source,
        )
        for group_id, category, parent_id, title, source in TriageGroup.objects.filter(
            triage__test_run_id=test_run_id
        )
        .order_by("position", "id")
        .values_list("id", "category", "parent_id", "title", "source")
    ]


def load_notes(test_run_id: int) -> dict[str, str]:
    notes = (
        RunTriage.objects.filter(test_run_id=test_run_id)
        .values_list("category_notes", flat=True)
        .first()
    )
    if not isinstance(notes, dict):
        return {}
    return {str(key): str(value) for key, value in notes.items() if value}


def category_counts(rows: Iterable[RowView]) -> tuple[CategoryCount, ...]:
    count: Counter[str] = Counter()
    critical: Counter[str] = Counter()
    untested: Counter[str] = Counter()
    for row in rows:
        if row.untested:
            untested[row.category] += 1
            continue
        count[row.category] += 1
        if row.severity == _CRITICAL:
            critical[row.category] += 1
    return tuple(
        CategoryCount(
            category=category,
            label=category_label(category),
            count=count[category],
            critical=critical[category],
            untested=untested[category],
        )
        for category in CATEGORY_ORDER
        if count[category] or untested[category]
    )


def counts_line(counts: Sequence[CategoryCount]) -> str:
    if not counts:
        return "No case is classified yet."
    return "Per category: " + "; ".join(
        f"{item.label} {count_suffix(item.count, item.critical, item.untested)}"
        for item in counts
    )


def thread_state(test_run_id: int) -> tuple[int | None, bool]:
    """(the run's active analysis thread, whether its turn is live); one query."""
    live = SetupTurn.objects.filter(
        live_turn_q(timezone.now()), conversation=OuterRef("pk")
    )
    found = (
        SetupConversation.objects.filter(
            kind=SetupConversationKind.RUN_TRIAGE,
            triage_run_id=test_run_id,
            archived_at__isnull=True,
        )
        .annotate(turn_live=Exists(live))
        .values_list("id", "turn_live")
        .first()
    )
    if found is None:
        return None, False
    return found[0], bool(found[1])
