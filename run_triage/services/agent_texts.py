"""Plain-text reads of the case analysis for the analysis agent's tools."""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Final

from django.conf import settings
from django.db.models.functions import Length

from projects.models import TestRun, TestRunScreenshot, TestRunTestCase
from projects.testrail_evidence import OVERLAY_TOOL_NAME
from run_triage.case_queries import analysable_q
from run_triage.case_refs import case_ref, pivot_ref, pivot_test_id, resolve_case_ref
from run_triage.exceptions import RunTriageError
from run_triage.models import TriageCase
from run_triage.services.taxonomy_text import (
    CATEGORY_ORDER,
    category_label,
    severity_label,
    severity_rank,
)
from run_triage.services.triage_input import (
    case_text_of,
    status_label_of,
    verdict_text_of,
)
from run_triage.services.triage_state import (
    NOT_GROUPED,
    GroupView,
    RowView,
    category_counts,
    counts_line,
    is_stale,
    load_groups,
    load_notes,
    load_state,
)
from run_triage.taxonomy import TriageSource
from run_triage.tool_text import count_suffix, cut, fenced, one_line, plural

# Screenshots listed by ``triage_case_text`` (newest first).
_CASE_SCREENSHOTS_SHOWN: Final = 30


def count_analysable(test_run_id: int) -> int:
    """Cases of the run the analysis classifies (one query)."""
    return TestRunTestCase.objects.filter(
        analysable_q(), test_run_id=test_run_id
    ).count()


def category_counts_text(test_run_id: int) -> str:
    """ "Per category: …" over the run's rows (one query)."""
    rows = TriageCase.objects.filter(triage__test_run_id=test_run_id).values_list(
        "category", "severity", "untested"
    )
    return counts_line(
        category_counts(
            RowView(
                pivot_id=0,
                category=category,
                severity=severity,
                finding="",
                untested=untested,
                group_id=None,
                source="",
                corrected_by_label="",
                stale=False,
            )
            for category, severity, untested in rows
        )
    )


def describe_triage_status(test_run_id: int) -> str:
    """One line for the analysis role's status (two queries)."""
    name = _run_name(test_run_id)
    state = load_state(test_run_id)
    return (
        f"Run {name}: {state.analysable} analysable, "
        f"{len(state.rows)} classified, {state.stale} out of date, "
        f"{state.unclassified} unclassified."
    )


def _run_name(test_run_id: int) -> str:
    name = TestRun.objects.filter(id=test_run_id).values_list("name", flat=True).first()
    if name is None:
        raise RunTriageError("This run no longer exists.")
    return name or f"Run #{test_run_id}"


def triage_overview_text(test_run_id: int) -> str:
    """Counts by status and category, unclassified / out of date / corrected,
    and the group outline (titles and counts, not cases); four queries."""
    name = _run_name(test_run_id)
    state = load_state(test_run_id)
    head = (
        f"{state.total} cases: {state.passed} passed, {state.analysable} "
        f"analysable, {state.not_finished} not finished. Classified "
        f"{len(state.rows)}; {state.unclassified} unclassified; {state.stale} "
        f"out of date; {state.corrected} corrected by a person."
    )
    outline = _outline(
        state.rows, load_groups(test_run_id), load_notes(test_run_id), None
    )
    return "\n".join(
        [
            fenced(f"Run: {one_line(name)}"),
            head,
            counts_line(category_counts(state.rows)),
            outline,
        ]
    )


def triage_outline_text(test_run_id: int, categories: Collection[str] | None) -> str:
    """The group outline of ``categories`` (all when None); three queries."""
    state = load_state(test_run_id)
    return _outline(
        state.rows, load_groups(test_run_id), load_notes(test_run_id), categories
    )


def _outline(
    rows: Sequence[RowView],
    groups: Sequence[GroupView],
    notes: dict[str, str],
    categories: Collection[str] | None,
) -> str:
    per_group: Counter[int] = Counter()
    ungrouped: Counter[str] = Counter()
    group_ids = {group.id for group in groups}
    for row in rows:
        if row.group_id is not None and row.group_id in group_ids:
            per_group[row.group_id] += 1
        else:
            ungrouped[row.category] += 1
    children: dict[int, list[GroupView]] = {}
    for group in groups:
        if group.parent_id is not None:
            children.setdefault(group.parent_id, []).append(group)
    counts = category_counts(rows)
    lines: list[str] = []
    number = 0
    for item in counts:
        number += 1
        if categories is not None and item.category not in categories:
            continue
        lines.append(
            f"{number}. {item.label} [{item.category}] "
            f"{count_suffix(item.count, item.critical, item.untested)}"
        )
        note = notes.get(item.category, "")
        if note:
            lines.append(f"   Note: {one_line(note)}")
        for group in groups:
            if group.category != item.category or group.parent_id is not None:
                continue
            subs = children.get(group.id, [])
            total = per_group[group.id] + sum(per_group[sub.id] for sub in subs)
            marker = " (person)" if group.source == TriageSource.PERSON else ""
            lines.append(f"   - {one_line(group.title)}{marker}: {total}")
            for sub in subs:
                lines.append(f"     - {one_line(sub.title)}: {per_group[sub.id]}")
        if ungrouped[item.category]:
            lines.append(f"   - {NOT_GROUPED}: {ungrouped[item.category]}")
    if not lines:
        return "No groups yet."
    return "Outline:\n" + fenced("\n".join(lines))


@dataclass(frozen=True)
class _ListRow:
    ref: str
    severity: str
    group_path: str
    group_order: tuple[int, int, int, int]
    finding: str
    title: str
    flags: str


def list_triage_cases_text(test_run_id: int, category: str, page: int) -> str:
    """One page of a category's cases as compact lines (one query)."""
    if category not in CATEGORY_ORDER:
        raise RunTriageError(
            f"Unknown category {category!r}; use one of: " + ", ".join(CATEGORY_ORDER)
        )
    if page < 1:
        raise RunTriageError("page starts at 1.")
    rows = TriageCase.objects.filter(
        triage__test_run_id=test_run_id, category=category
    ).values_list(
        "test_run_test_case_id",
        "test_run_test_case__testrail_source_result_id",
        "test_run_test_case__testrail_test_id",
        "test_run_test_case__testrail_source_test_id",
        "test_run_test_case__status",
        "test_run_test_case__finished_at",
        "test_run_test_case__test_case__title",
        "severity",
        "finding",
        "untested",
        "source",
        "classified_status",
        "classified_finished_at",
        "group__title",
        "group__position",
        "group__id",
        "group__parent__title",
        "group__parent__position",
        "group__parent__id",
    )
    items: list[_ListRow] = []
    for (
        pivot_id,
        source_result_id,
        test_id,
        source_test_id,
        status,
        finished_at,
        title,
        severity,
        finding,
        untested,
        source,
        classified_status,
        classified_finished_at,
        group_title,
        group_position,
        group_id,
        parent_title,
        parent_position,
        parent_id,
    ) in rows:
        if parent_title is not None:
            path = f"{one_line(parent_title)} › {one_line(group_title or '')}"
            order = (0, parent_position or 0, parent_id or 0, group_position or 0)
        elif group_title is not None:
            path = one_line(group_title)
            order = (0, group_position or 0, group_id or 0, -1)
        else:
            path = NOT_GROUPED
            order = (1, 0, 0, 0)
        flags = [
            label
            for label, on in (
                (
                    "out of date",
                    is_stale(
                        classified_status, classified_finished_at, status, finished_at
                    ),
                ),
                ("untested", untested),
                ("corrected by a person", source == TriageSource.PERSON),
            )
            if on
        ]
        items.append(
            _ListRow(
                ref=case_ref(
                    pivot_id, pivot_test_id(source_result_id, test_id, source_test_id)
                ),
                severity=severity,
                group_path=path,
                group_order=order,
                finding=one_line(finding),
                title=one_line(title),
                flags=f" | {', '.join(flags)}" if flags else "",
            )
        )
    items.sort(key=lambda item: (item.group_order, severity_rank(item.severity)))
    label = category_label(category)
    if not items:
        return f"{label}: no cases."
    size = settings.RUN_TRIAGE_LIST_PAGE_SIZE
    pages = (len(items) + size - 1) // size
    if page > pages:
        return f"{label}: no cases on page {page}; there are {plural(pages, 'page')}."
    shown = items[(page - 1) * size : page * size]
    body = "\n".join(
        f"{item.ref} | {item.severity} | {item.group_path} | {item.finding} | "
        f"{item.title}{item.flags}"
        for item in shown
    )
    lines = [
        f"{label}: {plural(len(items), 'case')}, page {page} of {pages}. "
        "Columns: case | severity | group | finding | title | flags.",
        fenced(body),
    ]
    if page < pages:
        lines.append(
            f'Next page: list_triage_cases(category="{category}", page={page + 1}).'
        )
    return "\n".join(lines)


def triage_case_text(test_run_id: int, ref: str) -> str:
    """One case in full: case text, verdict, steps, its analysis, screenshot
    ids with step numbers and the log size (three queries after the ref)."""
    pivot_id = resolve_case_ref(test_run_id, ref)
    pivot = (
        TestRunTestCase.objects.select_related("test_case", "retest_marked_by")
        .defer("logs")
        .annotate(log_chars=Length("logs"))
        .filter(id=pivot_id, test_run_id=test_run_id)
        .first()
    )
    if pivot is None:
        raise RunTriageError(f"{ref} is not a case of this run.")
    row = (
        TriageCase.objects.select_related("group", "group__parent")
        .filter(triage__test_run_id=test_run_id, test_run_test_case_id=pivot_id)
        .first()
    )
    shots = list(
        TestRunScreenshot.objects.filter(test_run_test_case_id=pivot_id)
        .exclude(tool_name=OVERLAY_TOOL_NAME)
        .order_by("-created_at", "-id")
        .values_list("id", "step_num")[:_CASE_SCREENSHOTS_SHOWN]
    )
    budget = settings.RUN_TRIAGE_CASE_INPUT_MAX_CHARS
    finished = (
        pivot.finished_at.strftime("%Y-%m-%d %H:%M UTC") if pivot.finished_at else "—"
    )
    raw_log_chars = getattr(pivot, "log_chars", 0)
    log_chars = raw_log_chars if isinstance(raw_log_chars, int) else 0
    body = "\n\n".join(
        [
            f"Case {pivot_ref(pivot)} (run case {pivot.id}): {pivot.test_case.title}",
            f"Status: {status_label_of(pivot)}; finished {finished}",
            "CASE TEXT:\n" + cut(case_text_of(pivot.test_case), budget),
            "RESULT AND STEPS:\n" + (cut(verdict_text_of(pivot), budget) or "(none)"),
        ]
    )
    lines = [fenced(body), _row_text(row, pivot)]
    if shots:
        listed = ", ".join(
            f"#{shot_id}" + (f" (step {step})" if step is not None else "")
            for shot_id, step in shots
        )
        lines.append(f"Screenshots, newest first: {listed}.")
    else:
        lines.append("No screenshots.")
    lines.append(
        f"Log: {log_chars} characters (read pages with read_case_log)."
        if log_chars
        else "Log: empty."
    )
    return "\n".join(lines)


def _row_text(row: TriageCase | None, pivot: TestRunTestCase) -> str:
    if row is None:
        return "Analysis: not classified yet."
    group = row.group
    if group is None:
        path = NOT_GROUPED
    elif group.parent is not None:
        path = f"{one_line(group.parent.title)} › {one_line(group.title)}"
    else:
        path = one_line(group.title)
    notes: list[str] = []
    if is_stale(
        row.classified_status,
        row.classified_finished_at,
        pivot.status,
        pivot.finished_at,
    ):
        notes.append("out of date: the case changed since it was classified")
    if row.untested:
        notes.append("untested")
    if row.source == TriageSource.PERSON:
        notes.append(f"corrected by {row.corrected_by_label or 'a person'}")
    extra = f" ({'; '.join(notes)})" if notes else ""
    return "Analysis:\n" + fenced(
        f"{category_label(row.category)} [{row.category}] / "
        f"{severity_label(row.severity)} — {one_line(row.finding)}\n"
        f"Group: {path}{extra}"
    )
