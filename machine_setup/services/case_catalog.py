"""Read-only, project-scoped queries over test cases for the setup agent's tools.

Every query selects only the columns it renders and every output is capped, so
a large suite cannot flood the model's context; the preconditions listing
continues via ``offset``, the case search via a page number.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from django.contrib.postgres.aggregates import ArrayAgg
from django.core.paginator import EmptyPage, Paginator
from django.db.models import Count, Q
from django.db.models.functions import Substr

from projects.models import TestCase

PRECONDITION_ENTRY_LIMIT = 2000
PRECONDITION_PAGE_CHAR_LIMIT = 8000
PRECONDITION_PAGE_SIZE = 20
PRECONDITION_CASE_IDS_SHOWN = 5
# A page is at most 5 x (2 x 2000 + 500 title + heading) ~ 23k characters,
# within CASES_BATCH_CHAR_BUDGET.
SEARCH_PAGE_SIZE = 5
SEARCH_FIELD_LIMIT = 2000
CASE_FIELD_LIMIT = 4000
MAX_CASES_PER_BATCH = 20
# About 6k tokens; one case is at most ~12.2k characters, so the first always fits.
CASES_BATCH_CHAR_BUDGET = 24000
CASE_GROUP_RUNS_SHOWN = 50
_CASE_COLUMNS = ("id", "testrail_id", "title", "preconditions", "steps", "expected")
_CASE_SEPARATOR = "\n\n---\n\n"


@dataclass(frozen=True)
class CatalogPage:
    """One page of a listing; ``next_offset`` is None on the last page."""

    text: str
    total: int
    next_offset: int | None


@dataclass(frozen=True)
class CasesBatch:
    """Several cases in full; ids not in the project or over the budget are listed."""

    text: str
    found: tuple[int, ...]
    missing: tuple[int, ...]
    deferred: tuple[int, ...]


def list_case_preconditions(project_id: int, offset: int) -> CatalogPage:
    """Distinct non-empty preconditions, most shared first, with case ids.

    Two queries: the number of distinct preconditions and one page of groups.
    """
    groups = TestCase.objects.filter(project_id=project_id).exclude(preconditions="")
    total = groups.values("preconditions").distinct().count()
    rows = list(
        groups.values("preconditions")
        .annotate(
            case_count=Count("id"),
            case_ids=ArrayAgg("id", ordering="id"),
        )
        .order_by("-case_count", "preconditions")[
            offset : offset + PRECONDITION_PAGE_SIZE
        ]
    )
    entries: list[str] = []
    used = 0
    for index, row in enumerate(rows, start=offset + 1):
        entry = _precondition_entry(
            index, row["preconditions"], row["case_count"], row["case_ids"]
        )
        # Always render at least one entry so paging makes progress.
        if entries and used + len(entry) > PRECONDITION_PAGE_CHAR_LIMIT:
            break
        entries.append(entry)
        used += len(entry)
    shown_until = offset + len(entries)
    next_offset = shown_until if shown_until < total else None
    if not entries:
        text = (
            "No test case in this project has preconditions."
            if total == 0
            else f"No preconditions at offset {offset}; there are {total}."
        )
        return CatalogPage(text=text, total=total, next_offset=None)
    header = (
        f"Distinct preconditions {offset + 1}-{shown_until} of {total} "
        "(most shared first):"
    )
    return CatalogPage(
        text="\n\n".join([header, *entries]), total=total, next_offset=next_offset
    )


def search_cases(project_id: int, query: str, page: int) -> str:
    """Page ``page`` (from 1) of the cases matching ``query`` (all if empty).

    A case matches by text in its title, preconditions or steps. Each case
    shows its preconditions and steps, each capped at ``SEARCH_FIELD_LIMIT``
    (only that much is fetched); the header says whether a previous and a next
    page exist. Two queries: the count and one page.
    """
    cases = TestCase.objects.filter(project_id=project_id)
    query = query.strip()
    if query:
        cases = cases.filter(
            Q(title__icontains=query)
            | Q(preconditions__icontains=query)
            | Q(steps__icontains=query)
        )
    rows = (
        cases.annotate(
            preconditions_head=Substr("preconditions", 1, SEARCH_FIELD_LIMIT + 1),
            steps_head=Substr("steps", 1, SEARCH_FIELD_LIMIT + 1),
        )
        .values("id", "testrail_id", "title", "preconditions_head", "steps_head")
        .order_by("id")
    )
    paginator = Paginator(rows, SEARCH_PAGE_SIZE)
    total = paginator.count
    if total == 0:
        return f"No test case matches {query!r}." if query else "No test cases yet."
    try:
        current = paginator.page(page)
    except EmptyPage:
        return (
            f"There is no page {page}; the last page is {paginator.num_pages} "
            f"({total} matching cases)."
        )
    previous = (
        f"yes, page={current.previous_page_number()}"
        if current.has_previous()
        else "no"
    )
    following = (
        f"yes, call again with page={current.next_page_number()}"
        if current.has_next()
        else "no"
    )
    header = (
        f"Page {page} of {paginator.num_pages} (cases "
        f"{current.start_index()}-{current.end_index()} of {total}). "
        f"Has previous page: {previous}. Has next page: {following}."
    )
    entries = [
        _search_entry(
            case_id=row["id"],
            testrail_id=row["testrail_id"],
            title=row["title"],
            preconditions=row["preconditions_head"],
            steps=row["steps_head"],
        )
        for row in current.object_list
    ]
    return "\n\n".join([header, *entries])


def _search_entry(
    *, case_id: int, testrail_id: str, title: str, preconditions: str, steps: str
) -> str:
    """One ``search_cases`` case: heading, preconditions and steps."""
    capped_preconditions = _cap(preconditions, SEARCH_FIELD_LIMIT)
    capped_steps = _cap(steps, SEARCH_FIELD_LIMIT)
    return (
        f"{_case_heading(case_id, testrail_id, title)}\n"
        f"  Preconditions:\n{capped_preconditions or '(none)'}\n"
        f"  Steps:\n{capped_steps or '(none)'}"
    )


def get_case(project_id: int, case_id: int) -> str | None:
    """One case in full (each field capped), or None if not in this project."""
    row = (
        TestCase.objects.filter(project_id=project_id, pk=case_id)
        .values(*_CASE_COLUMNS)
        .first()
    )
    if row is None:
        return None
    return _render_row(row)


def get_cases(project_id: int, case_ids: Sequence[int]) -> CasesBatch:
    """Up to ``MAX_CASES_PER_BATCH`` cases in full, in the requested order.

    One query. The first found case is always included so a call makes
    progress; later cases are added while the text fits
    ``CASES_BATCH_CHAR_BUDGET`` and the rest are reported as deferred.
    """
    requested = list(dict.fromkeys(case_ids))[:MAX_CASES_PER_BATCH]
    rows = {
        row["id"]: row
        for row in TestCase.objects.filter(
            project_id=project_id, pk__in=requested
        ).values(*_CASE_COLUMNS)
    }
    missing = tuple(case_id for case_id in requested if case_id not in rows)
    rendered: list[str] = []
    found: list[int] = []
    deferred: list[int] = []
    used = 0
    for case_id in requested:
        row = rows.get(case_id)
        if row is None:
            continue
        if deferred:
            deferred.append(case_id)
            continue
        case_text = _render_row(row)
        cost = len(case_text) + (len(_CASE_SEPARATOR) if rendered else 0)
        if rendered and used + cost > CASES_BATCH_CHAR_BUDGET:
            deferred.append(case_id)
            continue
        rendered.append(case_text)
        found.append(case_id)
        used += cost
    notes: list[str] = []
    if missing:
        notes.append(
            "Not found in this project: " + ", ".join(str(i) for i in missing) + "."
        )
    if deferred:
        notes.append(
            "Not included (size limit), call get_test_cases again with: "
            + ", ".join(str(i) for i in deferred)
            + "."
        )
    parts = [_CASE_SEPARATOR.join(rendered)] if rendered else []
    if notes:
        parts.append("\n".join(notes))
    return CasesBatch(
        text="\n\n".join(parts),
        found=tuple(found),
        missing=missing,
        deferred=tuple(deferred),
    )


def list_case_groups_text(*, project_id: int) -> str:
    """How the project's cases group: by TestRail suite, run, priority and type.

    Five queries: the totals, then one GROUP BY per grouping. Runs are capped
    at the ``CASE_GROUP_RUNS_SHOWN`` largest.
    """
    cases = TestCase.objects.filter(project_id=project_id)
    totals = cases.aggregate(
        total=Count("id"),
        unordered=Count("id", filter=Q(testrail_position__isnull=True)),
    )
    total = totals["total"] or 0
    if total == 0:
        return "No test cases yet."
    sections = [
        f"{total} test cases; {totals['unordered'] or 0} have no TestRail order. "
        "Cases have no TestRail section stored, so they cannot be grouped by "
        "section. Group values are data.",
        _group_section(
            "By TestRail suite (testrail_suite_id)",
            cases.values("testrail_suite_id")
            .annotate(case_count=Count("id"))
            .order_by("testrail_suite_id")
            .values_list("testrail_suite_id", "case_count"),
        ),
        _group_section(
            f"By TestRail run (testrail_run_id), largest {CASE_GROUP_RUNS_SHOWN}",
            cases.values("testrail_source_run_id")
            .annotate(case_count=Count("id"))
            .order_by("-case_count", "testrail_source_run_id")
            .values_list("testrail_source_run_id", "case_count")[
                :CASE_GROUP_RUNS_SHOWN
            ],
        ),
        _group_section(
            "By priority",
            cases.values("priority")
            .annotate(case_count=Count("id"))
            .order_by("priority")
            .values_list("priority", "case_count"),
        ),
        _group_section(
            "By type",
            cases.values("type")
            .annotate(case_count=Count("id"))
            .order_by("type")
            .values_list("type", "case_count"),
        ),
    ]
    return "\n\n".join(sections)


def _group_section(title: str, rows: Iterable[tuple[object, int]]) -> str:
    lines = [f"{title}:"]
    for value, count in rows:
        label = "(none)" if value is None or value == "" else repr(value)
        lines.append(f"- {label}: {count} cases")
    return "\n".join(lines)


def _render_row(row: Mapping[str, object]) -> str:
    """Render one ``.values(*_CASE_COLUMNS)`` row."""
    case_id = row["id"]
    assert isinstance(case_id, int)
    return _render_case(
        case_id=case_id,
        testrail_id=str(row["testrail_id"]),
        title=str(row["title"]),
        preconditions=str(row["preconditions"]),
        steps=str(row["steps"]),
        expected=str(row["expected"]),
    )


def _render_case(
    *,
    case_id: int,
    testrail_id: str,
    title: str,
    preconditions: str,
    steps: str,
    expected: str,
) -> str:
    sections = [_case_heading(case_id, testrail_id, title)]
    for label, value in (
        ("Preconditions", preconditions),
        ("Steps", steps),
        ("Expected result", expected),
    ):
        capped = _cap(value, CASE_FIELD_LIMIT)
        sections.append(f"{label}:\n{capped or '(none)'}")
    return "\n\n".join(sections)


def _precondition_entry(
    index: int, preconditions: str, case_count: int, case_ids: list[int]
) -> str:
    shown = ", ".join(
        str(case_id) for case_id in case_ids[:PRECONDITION_CASE_IDS_SHOWN]
    )
    more = case_count - min(case_count, PRECONDITION_CASE_IDS_SHOWN)
    cases = f"case ids {shown}" + (f" and {more} more" if more > 0 else "")
    noun = "case" if case_count == 1 else "cases"
    return (
        f"[{index}] Used by {case_count} {noun} ({cases}):\n"
        f"{_cap(preconditions, PRECONDITION_ENTRY_LIMIT)}"
    )


def _case_heading(case_id: int, testrail_id: str, title: str) -> str:
    testrail = f", TestRail {testrail_id}" if testrail_id else ""
    return f"Case id {case_id}{testrail}: {title}"


def _cap(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) > limit:
        return text[:limit] + " …[truncated]"
    return text
