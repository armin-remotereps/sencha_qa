"""Resolve a proposed run's selectors into the project's case ids."""

from __future__ import annotations

from django.db.models import Q

from machine_setup.run_proposal_types import (
    MAX_CASES_PER_PROPOSED_RUN,
    MAX_IDS_PER_LIST,
    SAMPLE_TITLES,
    ResolvedSelection,
    RunSpec,
)
from projects.models import TestCase
from projects.run_case_order import PICKER_CASE_ORDERING


def resolve_selection(*, project_id: int, spec: RunSpec) -> ResolvedSelection:
    """The cases ``spec`` selects in this project, in picker order.

    One ``values("id", "title")`` query. Semantics are those of ``RunSpec``:
    the filters are ANDed (``all_cases`` alone selects every case), the
    explicit ``case_ids`` and ``testrail_case_ids`` are unioned with that set,
    and ``exclude_case_ids`` is removed last. Explicit ids outside the project
    are simply not found.

    At most ``MAX_CASES_PER_PROPOSED_RUN + 1`` ids are fetched, so memory stays
    bounded: a longer list than the limit means "over the limit", not an
    exact count.
    """
    cases = TestCase.objects.filter(project_id=project_id)
    filtered = _filters_q(spec)
    if not (spec.all_cases and filtered is None):
        # Not "every case": the filters and the explicit ids, unioned.
        explicit = _explicit_q(spec)
        parts = [q for q in (filtered, explicit) if q is not None]
        if not parts:
            return ResolvedSelection(case_ids=[], sample_titles=[])
        cases = cases.filter(parts[0] | parts[1] if len(parts) == 2 else parts[0])
    excluded = list(spec.exclude_case_ids[:MAX_IDS_PER_LIST])
    if excluded:
        cases = cases.exclude(id__in=excluded)
    rows = list(
        cases.order_by(*PICKER_CASE_ORDERING).values("id", "title")[
            : MAX_CASES_PER_PROPOSED_RUN + 1
        ]
    )
    return ResolvedSelection(
        case_ids=[row["id"] for row in rows],
        sample_titles=[row["title"] for row in rows[:SAMPLE_TITLES]],
    )


def _filters_q(spec: RunSpec) -> Q | None:
    """The ANDed filters, or None when the spec has none."""
    parts: list[Q] = []
    title = spec.title_contains.strip()
    if title:
        parts.append(Q(title__icontains=title))
    if spec.testrail_suite_id is not None:
        parts.append(Q(testrail_suite_id=spec.testrail_suite_id))
    if spec.testrail_run_id is not None:
        parts.append(Q(testrail_source_run_id=spec.testrail_run_id))
    if spec.priorities:
        parts.append(Q(priority__in=list(spec.priorities[:MAX_IDS_PER_LIST])))
    if spec.types:
        parts.append(Q(type__in=list(spec.types[:MAX_IDS_PER_LIST])))
    if not parts:
        return None
    combined = parts[0]
    for part in parts[1:]:
        combined &= part
    return combined


def _explicit_q(spec: RunSpec) -> Q | None:
    """Explicit case ids and TestRail ids, or None when there are none."""
    parts: list[Q] = []
    case_ids = list(spec.case_ids[:MAX_IDS_PER_LIST])
    if case_ids:
        parts.append(Q(id__in=case_ids))
    testrail_ids = _testrail_id_forms(spec.testrail_case_ids[:MAX_IDS_PER_LIST])
    if testrail_ids:
        parts.append(Q(testrail_id__in=testrail_ids))
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else parts[0] | parts[1]


def _testrail_id_forms(values: tuple[str, ...]) -> list[str]:
    """Each TestRail id in every stored form: "123", "C123" and "c123"."""
    forms: set[str] = set()
    for value in values:
        cleaned = value.strip()
        digits = cleaned[1:] if cleaned[:1] in ("C", "c") else cleaned
        if digits.isdigit():
            forms.update((digits, f"C{digits}", f"c{digits}"))
        elif cleaned:
            forms.add(cleaned)
    return sorted(forms)
