"""The case analysis report page's data (five queries plus the blocker's)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Final

from django.db.models import Count
from django.urls import reverse

from projects.models import Project, TestRun, TestRunTestCase
from projects.run_case_order import RUN_CASE_ORDERING
from projects.testrail_results_services import build_testrail_result_url
from run_triage.case_queries import analysable_q, has_result_annotation
from run_triage.case_refs import pivot_ref
from run_triage.case_scope import CaseScope
from run_triage.models import TriageCase
from run_triage.services.card import analyze_blocker_for
from run_triage.services.taxonomy_text import severity_label, severity_rank
from run_triage.services.triage_state import (
    GroupView,
    RowView,
    category_counts,
    is_stale,
    load_groups,
    load_notes,
    scope_of,
    thread_state,
)
from run_triage.taxonomy import TriageSource
from run_triage.types import (
    ReportCase,
    ReportCategory,
    ReportGroup,
    TriageActions,
    TriageReport,
)

_REPORT_PIVOT_FIELDS: Final = (
    "id",
    "test_run",
    "test_case",
    "status",
    "finished_at",
    "testrail_source_result_id",
    "testrail_test_id",
    "testrail_source_test_id",
    "testrail_source_run_id",
    "testrail_result_id",
    "test_case__title",
    "test_case__testrail_id",
)


def get_triage_report(test_run: TestRun, project: Project) -> TriageReport:
    """The whole report (six queries: scope counts, analysable cases, rows,
    groups, notes, thread). Passed and unfinished cases are only counted."""
    scopes: Counter[CaseScope] = Counter()
    for status, has_result, count in (
        TestRunTestCase.objects.filter(test_run_id=test_run.id)
        .annotate(has_result=has_result_annotation())
        .values_list("status", "has_result")
        .annotate(count=Count("id"))
        .order_by()
    ):
        scopes[scope_of(status, bool(has_result))] += count
    pivots = list(
        TestRunTestCase.objects.filter(analysable_q(), test_run_id=test_run.id)
        .select_related("test_case")
        .only(*_REPORT_PIVOT_FIELDS)
        .annotate(has_result=has_result_annotation())
        .order_by(*RUN_CASE_ORDERING)
    )
    rows = {
        pivot_id: (
            category,
            severity,
            finding,
            untested,
            group_id,
            source,
            corrected_by_label,
            classified_status,
            classified_finished_at,
        )
        for (
            pivot_id,
            category,
            severity,
            finding,
            untested,
            group_id,
            source,
            corrected_by_label,
            classified_status,
            classified_finished_at,
        ) in TriageCase.objects.filter(triage__test_run_id=test_run.id).values_list(
            "test_run_test_case_id",
            "category",
            "severity",
            "finding",
            "untested",
            "group_id",
            "source",
            "corrected_by_label",
            "classified_status",
            "classified_finished_at",
        )
    }
    groups = load_groups(test_run.id)
    notes = load_notes(test_run.id)
    thread_id, turn_live = thread_state(test_run.id)

    order: dict[int, int] = {}
    cases: dict[int, ReportCase] = {}
    row_views: list[RowView] = []
    unclassified: list[ReportCase] = []
    for index, pivot in enumerate(pivots):
        pivot.test_run = test_run
        has_result = bool(getattr(pivot, "has_result", False))
        scope = scope_of(pivot.status, has_result)
        order[pivot.id] = index
        row = rows.get(pivot.id)
        if row is None:
            if scope == CaseScope.ANALYSED:
                unclassified.append(_report_case(pivot, project, None))
            continue
        stale = is_stale(row[7], row[8], pivot.status, pivot.finished_at)
        view = RowView(
            pivot_id=pivot.id,
            category=row[0],
            severity=row[1],
            finding=row[2],
            untested=row[3],
            group_id=row[4],
            source=row[5],
            corrected_by_label=row[6],
            stale=stale,
        )
        row_views.append(view)
        cases[pivot.id] = _report_case(pivot, project, view)

    categories = _report_categories(row_views, cases, order, groups, notes)
    analysable = scopes[CaseScope.ANALYSED]
    return TriageReport(
        test_run_id=test_run.id,
        run_name=test_run.display_name,
        passed_count=scopes[CaseScope.PASSED],
        analysable_count=analysable,
        not_finished_count=scopes[CaseScope.NOT_FINISHED],
        classified_count=len(row_views),
        stale_count=sum(view.stale for view in row_views),
        unclassified_count=len(unclassified),
        turn_live=turn_live,
        categories=categories,
        unclassified=tuple(unclassified),
        actions=TriageActions(
            thread_id=thread_id,
            turn_live=turn_live,
            analyze_blocker=analyze_blocker_for(
                test_run, analysable=analysable, turn_live=turn_live
            ),
        ),
    )


def _report_case(
    pivot: TestRunTestCase, project: Project, row: RowView | None
) -> ReportCase:
    ref = pivot_ref(pivot)
    title = pivot.test_case.title
    url = build_testrail_result_url(pivot, project)
    if url:
        label = ref if ref.startswith("T") else title
    else:
        url = reverse(
            "projects:test_run_case_detail",
            kwargs={
                "project_id": project.id,
                "test_run_id": pivot.test_run_id,
                "pivot_id": pivot.id,
            },
        )
        label = title
    severity = row.severity if row is not None else ""
    return ReportCase(
        pivot_id=pivot.id,
        ref=ref,
        link_url=url,
        link_label=label,
        title=title,
        status_label=str(pivot.get_status_display()),
        severity=severity,
        severity_label=severity_label(severity) if severity else "",
        finding=row.finding if row is not None else "",
        untested=row.untested if row is not None else False,
        stale=row.stale if row is not None else False,
        corrected_by_label=(
            (row.corrected_by_label or "a person")
            if row is not None and row.source == TriageSource.PERSON
            else ""
        ),
    )


def _report_categories(
    rows: Sequence[RowView],
    cases: dict[int, ReportCase],
    order: dict[int, int],
    groups: Sequence[GroupView],
    notes: dict[str, str],
) -> tuple[ReportCategory, ...]:
    by_group: dict[int, list[int]] = {}
    group_by_id = {group.id: group for group in groups}
    ungrouped: dict[str, list[int]] = {}
    for row in rows:
        group = group_by_id.get(row.group_id) if row.group_id is not None else None
        if group is None or group.category != row.category:
            ungrouped.setdefault(row.category, []).append(row.pivot_id)
        else:
            by_group.setdefault(group.id, []).append(row.pivot_id)

    def sorted_cases(pivot_ids: Iterable[int]) -> tuple[ReportCase, ...]:
        return tuple(
            sorted(
                (cases[pivot_id] for pivot_id in pivot_ids),
                key=lambda case: (
                    severity_rank(case.severity),
                    order.get(case.pivot_id, 0),
                ),
            )
        )

    children: dict[int, list[GroupView]] = {}
    for group in groups:
        if group.parent_id is not None:
            children.setdefault(group.parent_id, []).append(group)

    result: list[ReportCategory] = []
    for number, count in enumerate(category_counts(rows), start=1):
        report_groups: list[ReportGroup] = []
        for group in groups:
            if group.category != count.category or group.parent_id is not None:
                continue
            subs = tuple(
                ReportGroup(
                    title=sub.title,
                    source=sub.source,
                    cases=sorted_cases(by_group.get(sub.id, [])),
                    sub_groups=(),
                )
                for sub in children.get(group.id, [])
                if by_group.get(sub.id)
            )
            report_group = ReportGroup(
                title=group.title,
                source=group.source,
                cases=sorted_cases(by_group.get(group.id, [])),
                sub_groups=subs,
            )
            if report_group.case_count:
                report_groups.append(report_group)
        result.append(
            ReportCategory(
                number=number,
                category=count.category,
                label=count.label,
                count=count.count,
                critical=count.critical,
                untested=count.untested,
                note=notes.get(count.category, ""),
                groups=tuple(report_groups),
                ungrouped=sorted_cases(ungrouped.get(count.category, [])),
            )
        )
    return tuple(result)
