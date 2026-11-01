"""The body of the Manual run step guide task: one chunk per task.

A chunk is either the case's knowledge base research (once per case, as for
Human in the loop) or the next ``MANUAL_GUIDE_PARALLEL`` missing steps; each
chunk re-enqueues the next, so one guide never holds an ``assist`` slot for
long. Contexts (queries) and every write run in the main thread; only the
tool-free LLM calls run in a bounded thread pool. A step chunk has a wall-clock
budget (``MANUAL_GUIDE_CHUNK_TIMEOUT_SECONDS`` from its start): each call's
timeout is what is left of it, the chunk never waits past it, and a step not
written by then is stored failed. Every write is conditional on the guide's
``generation``: a superseded task writes nothing and stops. The guide's OS is
the one stored at claim time (``ManualStepGuide.os_name``), never the live one.

Logs carry ids, step numbers, counts and durations only: never step text,
how-to text or knowledge base content.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Final

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from agents.checkpoint import person_guide_from_json, person_guide_to_json
from agents.services.llm_config import build_llm_config
from agents.services.person_guide_context import (
    build_person_guide_context,
    refresh_person_guide_notes,
    research_due,
)
from agents.services.person_steps import prepare_case_step_how_to
from agents.types import (
    AgentCancelledError,
    LLMConfig,
    PersonGuideContext,
    PersonGuideNotes,
    PersonGuideSources,
    PersonHowToItem,
    SubTask,
)
from projects.case_task_text import build_task_description
from projects.manual_step_guide_read import (
    STEP_FAILED,
    STEP_READY,
    manual_plan,
    plan_fingerprint,
)
from projects.manual_step_guide_services import send_manual_guide_task
from projects.models import (
    ManualGuideState,
    ManualStepGuide,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.person_guide_sources import build_person_guide_sources
from projects.person_step_rows import how_to_rows
from projects.runner_person_step_push import nudge_runner_person_steps

logger = logging.getLogger(__name__)

_THREAD_PREFIX: Final = "manual-guide"
# Stored beside the notes (``person_guide_from_json`` ignores it): the
# generation that ran the research, so a failed one is not retried at once.
_RESEARCH_GENERATION_KEY: Final = "generation"
# Each call's timeout ends this long before the chunk's budget, so a call
# that times out still lands before the chunk stops waiting.
_CALL_MARGIN_SECONDS: Final = 5.0
# Below this much budget (after the margin) a call is skipped: its answer would
# arrive too late to be used.
_MIN_CALL_TIMEOUT_SECONDS: Final = 1.0


@dataclass(frozen=True)
class _StepJob:
    """One step's call input, built in the main thread."""

    num: int
    task: SubTask
    guide: PersonGuideContext | None


def run_manual_guide_chunk(guide_id: int, generation: int) -> None:
    """Run one chunk of guide ``guide_id`` at ``generation``; never raises
    (a task-level error marks the guide ``failed``, written steps kept)."""
    try:
        _run_chunk(guide_id, generation)
    except Exception:
        logger.exception("Step guide %s: chunk failed", guide_id)
        # A soft time limit raised mid-query leaves the connection busy
        # ("another command is already in progress"): reconnect, or the
        # guide would stay WRITING until it reads stale.
        connection.close()
        project_id = _set_state(guide_id, generation, ManualGuideState.FAILED)
        if project_id is not None:
            _nudge_on_commit(project_id)


def _run_chunk(guide_id: int, generation: int) -> None:
    started = time.monotonic()
    # A chunk picked up late, or a slow one, must not read as stale: one
    # conditional write at its start (also the superseded check).
    if not ManualStepGuide.objects.filter(pk=guide_id, generation=generation).update(
        updated_at=timezone.now()
    ):
        return
    guide = (
        ManualStepGuide.objects.select_related(
            "test_run_test_case__test_case", "test_run_test_case__test_run"
        )
        .defer("test_run_test_case__logs")
        .filter(pk=guide_id)
        .first()
    )
    if guide is None or guide.generation != generation:
        return
    pivot = guide.test_run_test_case
    project_id = pivot.test_run.project_id
    plan = manual_plan(pivot)
    # The OS the claim fingerprinted (a disconnected runner reads as Linux).
    os_name = guide.os_name
    if (
        not _claimed(pivot)
        or not plan
        or plan_fingerprint(pivot, plan, os_name=os_name) != guide.fingerprint
    ):
        # Released, handed in or re-tested, or the case text changed:
        # missing steps stay missing; the next claim resumes them.
        _set_state(guide_id, generation, ManualGuideState.STOPPED)
        return
    task_description = build_task_description(pivot.test_case)
    notes = person_guide_from_json(guide.research) if guide.research else None
    sources = build_person_guide_sources(project_id)
    llm = build_llm_config()
    if _research(
        guide,
        notes,
        sources,
        llm=llm,
        task_description=task_description,
        os_name=os_name,
        project_id=project_id,
    ):
        return
    stored = guide.steps if isinstance(guide.steps, dict) else {}
    missing = [
        num
        for num in range(1, min(guide.step_count, len(plan)) + 1)
        if str(num) not in stored
    ]
    if not missing:
        if guide.state != ManualGuideState.DONE and (
            _set_state(guide_id, generation, ManualGuideState.DONE) is not None
        ):
            _nudge_on_commit(project_id)
        return
    chunk = missing[: settings.MANUAL_GUIDE_PARALLEL]
    if guide.state != ManualGuideState.WRITING and (
        _set_state(guide_id, generation, ManualGuideState.WRITING) is None
    ):
        return
    jobs = [
        _StepJob(
            num=num,
            task=plan[num - 1],
            guide=build_person_guide_context(
                notes,
                sources,
                related_text=_related_text(task_description, plan[num - 1]),
                on_log=None,
            ),
        )
        for num in chunk
    ]
    deadline = started + settings.MANUAL_GUIDE_CHUNK_TIMEOUT_SECONDS
    call = _step_caller(
        llm,
        task_description=task_description,
        total_steps=len(plan),
        system_info={"os": os_name},
        deadline=deadline,
    )
    # HTTP only in the pool (no ORM). Not a ``with`` block: its exit waits for
    # every call; a call past the budget ends on its own timeout, unused.
    pool = ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix=_THREAD_PREFIX)
    try:
        futures = [pool.submit(call, job) for job in jobs]
        wait(futures, timeout=max(0.0, deadline - time.monotonic()))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    outcomes = [_outcome(future) for future in futures]
    written = {str(job.num): _step_entry(items) for job, items in zip(jobs, outcomes)}
    logger.info(
        "Step guide %s: steps %s written in %.1fs (%s; %d past the budget)",
        guide_id,
        ",".join(str(job.num) for job in jobs),
        time.monotonic() - started,
        ",".join("failed" if items is None else str(len(items)) for items in outcomes),
        sum(not future.done() for future in futures),
    )
    remaining = len(missing) - len(chunk)
    if not _store_steps(guide_id, generation, written, done=remaining == 0):
        return
    _nudge_on_commit(project_id)
    if remaining:
        transaction.on_commit(lambda: send_manual_guide_task(guide_id, generation))


def _research(
    guide: ManualStepGuide,
    notes: PersonGuideNotes | None,
    sources: PersonGuideSources,
    *,
    llm: LLMConfig,
    task_description: str,
    os_name: str,
    project_id: int,
) -> bool:
    """Run the case's research when due; True when this chunk did (or the
    guide was superseded), so the caller stops."""
    guide_id, generation = guide.pk, guide.generation
    research = guide.research if isinstance(guide.research, dict) else {}
    if (
        settings.PERSON_GUIDE_RESEARCH_MAX_TOOL_ROUNDS <= 0
        or not research_due(notes)
        # A failed research is retried by a later claim, not right away.
        or research.get(_RESEARCH_GENERATION_KEY) == generation
        or sources.knowledge_index() is None
    ):
        return False
    if _set_state(guide_id, generation, ManualGuideState.RESEARCHING) is None:
        return True
    started = time.monotonic()
    try:
        researched = refresh_person_guide_notes(
            notes,
            sources,
            llm=llm,
            task_description=task_description,
            os_name=os_name,
            project_id=project_id,
            on_log=None,
            cancellation_check=_cancellation_check(guide_id, generation),
        )
    except AgentCancelledError:
        _set_state(guide_id, generation, ManualGuideState.STOPPED)
        return True
    if researched is None:
        return False
    logger.info(
        "Step guide %s: research %s in %.1fs",
        guide_id,
        researched.status,
        time.monotonic() - started,
    )
    with transaction.atomic():
        updated = ManualStepGuide.objects.filter(
            pk=guide_id, generation=generation
        ).update(
            research={
                **person_guide_to_json(researched),
                _RESEARCH_GENERATION_KEY: generation,
            },
            state=ManualGuideState.WRITING,
            updated_at=timezone.now(),
        )
        if updated:
            transaction.on_commit(lambda: send_manual_guide_task(guide_id, generation))
    return True


def _step_caller(
    llm: LLMConfig,
    *,
    task_description: str,
    total_steps: int,
    system_info: dict[str, object] | None,
    deadline: float,
) -> Callable[[_StepJob], tuple[PersonHowToItem, ...] | None]:
    """The pool's call: one step's items, None on any fault (no ORM here).
    Its timeout is what is left before ``deadline`` (``time.monotonic``),
    less a margin."""

    def call(job: _StepJob) -> tuple[PersonHowToItem, ...] | None:
        timeout = deadline - time.monotonic() - _CALL_MARGIN_SECONDS
        if timeout < _MIN_CALL_TIMEOUT_SECONDS:
            # Building the contexts used up the budget: the answer would be
            # discarded anyway, so skip the call.
            return None
        try:
            return prepare_case_step_how_to(
                llm,
                step_text=job.task.description,
                expected_result=job.task.expected_result,
                step_num=job.num,
                total_steps=total_steps,
                task_description=task_description,
                system_info=system_info,
                guide=job.guide,
                timeout_seconds=timeout,
            )
        except Exception as exc:
            # AIServiceError or anything else: no guide for this step.
            logger.warning(
                "Case step %d: how-to call failed (%s)", job.num, type(exc).__name__
            )
            return None

    return call


def _outcome(
    future: Future[tuple[PersonHowToItem, ...] | None],
) -> tuple[PersonHowToItem, ...] | None:
    """A call's items; None when it did not finish within the budget (or was
    cancelled). ``call`` itself never raises."""
    if not future.done() or future.cancelled():
        return None
    return future.result()


def _step_entry(items: tuple[PersonHowToItem, ...] | None) -> dict[str, object]:
    if items is None:
        return {"state": STEP_FAILED, "how_to": []}
    return {"state": STEP_READY, "how_to": how_to_rows(items)}


def _store_steps(
    guide_id: int, generation: int, written: Mapping[str, object], *, done: bool
) -> bool:
    """Merge ``written`` into the guide's steps (conditional on the
    generation); False when the guide was superseded."""
    with transaction.atomic():
        guide = (
            ManualStepGuide.objects.select_for_update()
            .filter(pk=guide_id, generation=generation)
            .only("steps", "state")
            .first()
        )
        if guide is None:
            return False
        steps = dict(guide.steps) if isinstance(guide.steps, dict) else {}
        steps.update(written)
        guide.steps = steps
        guide.state = ManualGuideState.DONE if done else ManualGuideState.WRITING
        guide.save(update_fields=["steps", "state", "updated_at"])
    return True


def _set_state(guide_id: int, generation: int, state: ManualGuideState) -> int | None:
    """Set the state when ``generation`` is current; the project id when it
    was written, None when the guide was superseded or deleted."""
    with transaction.atomic():
        updated = ManualStepGuide.objects.filter(
            pk=guide_id, generation=generation
        ).update(state=state, updated_at=timezone.now())
        if not updated:
            return None
        project_id: int | None = (
            ManualStepGuide.objects.filter(pk=guide_id)
            .values_list("test_run_test_case__test_run__project_id", flat=True)
            .first()
        )
    return project_id


def _cancellation_check(guide_id: int, generation: int) -> Callable[[], bool]:
    """True once the guide was superseded or the case lost its claim (one
    query, between research tool rounds)."""

    def cancelled() -> bool:
        return not (
            ManualStepGuide.objects.filter(
                pk=guide_id,
                generation=generation,
                test_run_test_case__status=TestRunTestCaseStatus.IN_PROGRESS,
            )
            .exclude(test_run_test_case__manual_claim_token="")
            .exists()
        )

    return cancelled


def _claimed(pivot: TestRunTestCase) -> bool:
    return pivot.status == TestRunTestCaseStatus.IN_PROGRESS and bool(
        pivot.manual_claim_token
    )


def _related_text(task_description: str, task: SubTask) -> str:
    """What a step's knowledge excerpts are looked up by, as for a Human in
    the loop step: the case title (first line), the step and its expected
    result."""
    title = next(
        (line.strip() for line in task_description.splitlines() if line.strip()), ""
    )
    return f"{title}\n{task.description}\n{task.expected_result}"


def _nudge_on_commit(project_id: int) -> None:
    transaction.on_commit(lambda: nudge_runner_person_steps(project_id))
