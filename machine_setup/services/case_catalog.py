"""Read-only, project-scoped queries over test cases for the setup agent's tools.

Every query selects only the columns it renders and every output is capped, so
a large suite cannot flood the model's context; pages continue via ``offset``.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.contrib.postgres.aggregates import ArrayAgg
from django.db.models import Count, Q
from django.db.models.functions import Substr

from projects.models import TestCase

PRECONDITION_ENTRY_LIMIT = 2000
PRECONDITION_PAGE_CHAR_LIMIT = 8000
PRECONDITION_PAGE_SIZE = 20
PRECONDITION_CASE_IDS_SHOWN = 5
SEARCH_PAGE_SIZE = 50
SEARCH_PRECONDITION_PREVIEW = 200
CASE_FIELD_LIMIT = 4000


@dataclass(frozen=True)
class CatalogPage:
    """One page of a listing; ``next_offset`` is None on the last page."""

    text: str
    total: int
    next_offset: int | None


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


def search_cases(project_id: int, query: str, offset: int) -> CatalogPage:
    """Cases whose title, preconditions or steps contain ``query`` (all if empty)."""
    cases = TestCase.objects.filter(project_id=project_id)
    query = query.strip()
    if query:
        cases = cases.filter(
            Q(title__icontains=query)
            | Q(preconditions__icontains=query)
            | Q(steps__icontains=query)
        )
    total = cases.count()
    rows = list(
        cases.annotate(
            preconditions_preview=Substr(
                "preconditions", 1, SEARCH_PRECONDITION_PREVIEW + 1
            )
        )
        .values("id", "testrail_id", "title", "preconditions_preview")
        .order_by("id")[offset : offset + SEARCH_PAGE_SIZE]
    )
    if not rows:
        if total == 0:
            text = f"No test case matches {query!r}." if query else "No test cases yet."
        else:
            text = f"No cases at offset {offset}; there are {total} matches."
        return CatalogPage(text=text, total=total, next_offset=None)
    shown_until = offset + len(rows)
    lines = [f"Cases {offset + 1}-{shown_until} of {total}:"]
    for row in rows:
        lines.append(_case_heading(row["id"], row["testrail_id"], row["title"]))
        preview = _cap(row["preconditions_preview"], SEARCH_PRECONDITION_PREVIEW)
        if preview:
            lines.append(f"  Preconditions: {preview}")
    return CatalogPage(
        text="\n".join(lines),
        total=total,
        next_offset=shown_until if shown_until < total else None,
    )


def get_case(project_id: int, case_id: int) -> str | None:
    """One case in full (each field capped), or None if not in this project."""
    row = (
        TestCase.objects.filter(project_id=project_id, pk=case_id)
        .values("id", "testrail_id", "title", "preconditions", "steps", "expected")
        .first()
    )
    if row is None:
        return None
    sections = [_case_heading(row["id"], row["testrail_id"], row["title"])]
    for label, value in (
        ("Preconditions", row["preconditions"]),
        ("Steps", row["steps"]),
        ("Expected result", row["expected"]),
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
