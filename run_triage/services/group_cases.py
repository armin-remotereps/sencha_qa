"""``group_triage_cases``: group each category's cases by root cause.

One grouper call per category (``RUN_TRIAGE_PARALLEL`` at a time) over
``(ref, severity, finding, title)`` only. A category's AI groups are replaced;
person-made groups and their cases are kept and not sent. Cases the answer
left out go to a code-made "Other findings" group. Each category is written
in its own transaction gated on the turn still running.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Final

from django.conf import settings
from django.db import transaction

from agents.exceptions import AIServiceError
from agents.services import run_case_grouper
from agents.services.llm_config import build_llm_config
from agents.triage_types import CategoryGrouping, GroupItem
from agents.types import LLMConfig
from projects.models import TestRun
from run_triage.case_refs import case_ref, pivot_test_id
from run_triage.exceptions import RunTriageError
from run_triage.models import RunTriage, TriageCase, TriageGroup
from run_triage.services.agent_texts import triage_outline_text
from run_triage.services.group_writes import (
    PlannedGroup,
    PlannedSubGroup,
    create_groups,
    next_position,
    write_note,
)
from run_triage.services.taxonomy_text import (
    CATEGORY_ORDER,
    category_entry,
    category_label,
)
from run_triage.services.turn_gate import require_running_turn
from run_triage.taxonomy import TriageSource
from run_triage.tool_deadline import DEADLINE_STOP_TEXT, StopReason, fits_before

OTHER_FINDINGS_TITLE: Final = "Other findings"


@dataclass(frozen=True)
class _CategoryWork:
    category: str
    # ref -> TriageCase id, for the cases sent to the grouper.
    row_ids: dict[str, int]
    items: tuple[GroupItem, ...]


def group_triage_cases(
    *,
    test_run_id: int,
    turn_id: int,
    categories: tuple[str, ...],
    should_stop: Callable[[], bool],
    deadline: float | None = None,
) -> str:
    """Group the named categories (all when empty); the outline text.

    ``deadline`` is a ``time.monotonic()`` value: no grouper call starts that
    could not finish before it (None: no deadline).
    """
    unknown = [category for category in categories if category not in CATEGORY_ORDER]
    if unknown:
        raise RunTriageError(
            "Unknown categories: "
            + ", ".join(unknown)
            + ". Use: "
            + ", ".join(CATEGORY_ORDER)
        )
    context = (
        TestRun.objects.filter(id=test_run_id)
        .values_list("project__project_prompt", flat=True)
        .first()
    )
    if context is None:
        raise RunTriageError("This run no longer exists.")
    triage_id = (
        RunTriage.objects.filter(test_run_id=test_run_id)
        .values_list("id", flat=True)
        .first()
    )
    if triage_id is None:
        return "Nothing to group: classify the cases first (classify_run_cases)."
    work = _work(triage_id, set(categories) if categories else None)
    if not work:
        return "Nothing to group: no classified case outside a person's groups."

    llm = build_llm_config()
    timeout = float(settings.RUN_TRIAGE_CALL_TIMEOUT_SECONDS)
    parallel = settings.RUN_TRIAGE_PARALLEL
    grouped: list[str] = []
    failed: list[str] = []
    stopped: StopReason = ""
    pool = ThreadPoolExecutor(max_workers=min(parallel, len(work)))
    try:
        for start in range(0, len(work), parallel):
            if should_stop():
                stopped = "stop"
                break
            if not fits_before(deadline, waves=1):
                stopped = "deadline"
                break
            batch = work[start : start + parallel]
            futures: list[Future[CategoryGrouping | None]] = [
                pool.submit(_group, llm, item, context, timeout) for item in batch
            ]
            for item, future in zip(batch, futures):
                result = future.result()
                if result is None:
                    failed.append(item.category)
                    continue
                _write_category(turn_id, triage_id, item, result)
                grouped.append(item.category)
    except BaseException:
        # Includes Celery's SoftTimeLimitExceeded: never wait for the calls.
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=True)

    remaining = len(work) - len(grouped) - len(failed)
    lines = [f"Grouped {len(grouped)} of {len(work)} categories."]
    if stopped == "deadline":
        lines.append(DEADLINE_STOP_TEXT.format(remaining=f"{remaining} categories"))
    elif stopped:
        lines.append(f"Stopped on request; {remaining} categories remain.")
    if failed:
        lines.append(
            "The AI service did not answer for: "
            + ", ".join(category_label(category) for category in failed)
            + "; their earlier groups are kept. Call again for them."
        )
    lines.append(triage_outline_text(test_run_id, [item.category for item in work]))
    return "\n".join(lines)


def _work(triage_id: int, wanted: set[str] | None) -> list[_CategoryWork]:
    """The cases each category sends to the grouper (one query)."""
    rows = TriageCase.objects.filter(triage_id=triage_id).values_list(
        "id",
        "test_run_test_case_id",
        "test_run_test_case__testrail_source_result_id",
        "test_run_test_case__testrail_test_id",
        "test_run_test_case__testrail_source_test_id",
        "test_run_test_case__test_case__title",
        "category",
        "severity",
        "finding",
        "group__source",
    )
    per_category: dict[str, list[tuple[str, int, GroupItem]]] = {}
    for (
        row_id,
        pivot_id,
        source_result_id,
        test_id,
        source_test_id,
        title,
        category,
        severity,
        finding,
        group_source,
    ) in rows:
        if wanted is not None and category not in wanted:
            continue
        if group_source == TriageSource.PERSON:
            continue
        ref = case_ref(
            pivot_id, pivot_test_id(source_result_id, test_id, source_test_id)
        )
        per_category.setdefault(category, []).append(
            (
                ref,
                row_id,
                GroupItem(ref=ref, severity=severity, finding=finding, title=title),
            )
        )
    return [
        _CategoryWork(
            category=category,
            row_ids={ref: row_id for ref, row_id, _ in per_category[category]},
            items=tuple(item for _, _, item in per_category[category]),
        )
        for category in CATEGORY_ORDER
        if category in per_category
    ]


def _group(
    llm: LLMConfig, work: _CategoryWork, context: str, timeout: float
) -> CategoryGrouping | None:
    """One grouper call; None when the AI did not answer. No ORM (worker thread)."""
    try:
        return run_case_grouper.group_category_cases(
            llm,
            category_entry(work.category),
            work.items,
            application_context=context,
            timeout_seconds=timeout,
        )
    except AIServiceError:
        return None


def _planned(work: _CategoryWork, grouping: CategoryGrouping) -> list[PlannedGroup]:
    """The answer's groups with each case once; the rest in "Other findings"."""
    seen: set[str] = set()

    def take(refs: Sequence[str]) -> tuple[int, ...]:
        ids: list[int] = []
        for ref in refs:
            if ref in work.row_ids and ref not in seen:
                seen.add(ref)
                ids.append(work.row_ids[ref])
        return tuple(ids)

    planned: list[PlannedGroup] = []
    for group in grouping.groups:
        if not group.title.strip():
            continue
        direct = take(group.refs)
        subs = tuple(
            PlannedSubGroup(title=sub.title, row_ids=ids)
            for sub in group.sub_groups
            if sub.title.strip() and (ids := take(sub.refs))
        )
        if direct or subs:
            planned.append(
                PlannedGroup(title=group.title, row_ids=direct, sub_groups=subs)
            )
    omitted = tuple(row_id for ref, row_id in work.row_ids.items() if ref not in seen)
    if omitted:
        planned.append(
            PlannedGroup(title=OTHER_FINDINGS_TITLE, row_ids=omitted, sub_groups=())
        )
    return planned


def _write_category(
    turn_id: int, triage_id: int, work: _CategoryWork, grouping: CategoryGrouping
) -> None:
    planned = _planned(work, grouping)
    with transaction.atomic():
        require_running_turn(turn_id)
        TriageGroup.objects.filter(
            triage_id=triage_id, category=work.category, source=TriageSource.AI
        ).delete()
        create_groups(
            triage_id=triage_id,
            category=work.category,
            groups=planned,
            source=str(TriageSource.AI),
            start_position=next_position(triage_id, work.category),
        )
        write_note(triage_id, work.category, grouping.note, grouped=True)
