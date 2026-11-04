"""``classify_run_cases``: classify a run's analysable cases in bounded batches.

One tool call runs at most ``RUN_TRIAGE_MAX_CALLS_PER_TOOL_CALL`` classifier
calls of ``RUN_TRIAGE_BATCH_SIZE`` cases, ``RUN_TRIAGE_PARALLEL`` at a time,
and says how many cases remain so the agent calls again. Queries, file reads
and writes run in the caller's thread; the worker threads only call the AI.
Each round's rows are written in one transaction gated on the turn still
running, so a stop keeps what was done and a force stop writes nothing more.

Which cases a call takes:

- ``pending``: no row yet, out of date (the case's status or finish time
  changed), or "Not classified" after an AI failure;
- ``all``: every analysable case, except a person's correction of a case
  that did not change;
- ``cases``: the named cases, always sent to the AI (a re-check).

A case whose input hash matches its row is not sent again (``pending`` and
``all``). Rows written earlier in this turn are skipped, so repeated calls
move on and an AI outage cannot loop.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from agents.exceptions import AIServiceError
from agents.services import run_case_classifier
from agents.services.llm_config import build_llm_config
from agents.triage_types import (
    CaseClassification,
    TriageCaseInput,
    TriageTaxonomyText,
)
from agents.types import LLMConfig
from machine_setup.models import SetupTurn
from projects.models import TestRun, TestRunTestCase
from run_triage.case_queries import analysable_q
from run_triage.case_refs import normalize_ref, run_ref_map
from run_triage.exceptions import RunTriageError
from run_triage.models import RunTriage, TriageCase
from run_triage.services.agent_texts import category_counts_text
from run_triage.services.taxonomy_text import (
    CATEGORY_ORDER,
    SEVERITY_ORDER,
    triage_taxonomy,
)
from run_triage.services.triage_input import (
    BUILD_CHUNK_SIZE,
    CaseDraft,
    build_case_drafts,
    load_case_inputs,
)
from run_triage.services.turn_gate import require_running_turn
from run_triage.taxonomy import (
    TRIAGE_FINDING_MAX_LENGTH,
    TRIAGE_PROMPT_VERSION,
    TriageCategory,
    TriageSeverity,
    TriageSource,
)
from run_triage.tool_deadline import DEADLINE_STOP_TEXT, StopReason, fits_before
from run_triage.tool_text import cut, one_line, plural

ClassifyScope = Literal["pending", "all", "cases"]

NOT_CLASSIFIED_FINDING: Final = "Not classified: the AI service did not answer"
# Refs of unclassified cases named in one result.
_FAILED_REFS_SHOWN: Final = 20

_ROW_FIELDS: Final = (
    "category",
    "severity",
    "finding",
    "untested",
    "group",
    "input_hash",
    "classified_status",
    "classified_finished_at",
    "source",
    "corrected_by_label",
    "corrected_at",
    "updated_at",
)
_REUSE_FIELDS: Final = (
    "untested",
    "classified_status",
    "classified_finished_at",
    "updated_at",
)


@dataclass(frozen=True)
class _Row:
    pivot_id: int
    input_hash: str
    classified_status: str
    classified_finished_at: datetime | None
    source: str
    updated_at: datetime


@dataclass(frozen=True)
class _Case:
    pivot_id: int
    status: str
    finished_at: datetime | None


@dataclass(frozen=True)
class _Outcome:
    draft: CaseDraft
    # None: the AI did not answer for this case.
    classification: CaseClassification | None


def classify_run_cases(
    *,
    test_run_id: int,
    turn_id: int,
    scope: ClassifyScope,
    refs: tuple[str, ...],
    should_stop: Callable[[], bool],
    deadline: float | None = None,
) -> str:
    """Classify the next cases of the run; the tool result text.

    ``deadline`` is a ``time.monotonic()`` value: no AI call starts that
    could not finish before it (None: no deadline).
    """
    run = _load_run(test_run_id)
    cases = _analysable_cases(test_run_id)
    if len(cases) > settings.RUN_TRIAGE_MAX_CASES:
        raise RunTriageError(
            f"This run has {len(cases)} cases to analyse; at most "
            f"{settings.RUN_TRIAGE_MAX_CASES} can be analysed."
        )
    rows = _existing_rows(test_run_id)
    named = _named_cases(test_run_id, scope, refs, cases)
    turn_started_at = _turn_started_at(turn_id)
    gone = [pivot_id for pivot_id in rows if pivot_id not in cases]
    candidates = _candidates(cases, rows, scope, named, turn_started_at)

    reused: list[CaseDraft] = []
    to_call: list[CaseDraft] = []
    budget = max_named_refs()
    for start in range(0, len(candidates), BUILD_CHUNK_SIZE):
        if len(to_call) >= budget or should_stop():
            break
        for draft in build_case_drafts(
            test_run_id, candidates[start : start + BUILD_CHUNK_SIZE]
        ):
            if len(to_call) >= budget:
                break
            row = rows.get(draft.pivot_id)
            if (
                scope != "cases"
                and row is not None
                and row.input_hash
                and row.input_hash == draft.input_hash
            ):
                reused.append(draft)
            else:
                to_call.append(draft)
        else:
            continue
        break

    triage_id = _write_reused(
        turn_id=turn_id, test_run_id=test_run_id, reused=reused, gone=gone
    )
    outcomes, stopped = _classify(
        run, triage_id, turn_id, to_call, should_stop, deadline
    )

    failed = [o.draft.ref for o in outcomes if o.classification is None]
    done = len(reused) + len(outcomes)
    remaining = len(candidates) - done
    return _summary(
        test_run_id=test_run_id,
        done=done,
        reused=len(reused),
        failed=failed,
        remaining=remaining,
        stopped=stopped,
        nothing_to_do=not candidates,
    )


def _load_run(test_run_id: int) -> TestRun:
    run = (
        TestRun.objects.select_related("project")
        .only("id", "project__id", "project__project_prompt")
        .filter(id=test_run_id)
        .first()
    )
    if run is None:
        raise RunTriageError("This run no longer exists.")
    return run


def _analysable_cases(test_run_id: int) -> dict[int, _Case]:
    return {
        pivot_id: _Case(pivot_id=pivot_id, status=status, finished_at=finished_at)
        for pivot_id, status, finished_at in TestRunTestCase.objects.filter(
            analysable_q(), test_run_id=test_run_id
        )
        .order_by("id")
        .values_list("id", "status", "finished_at")
    }


def _existing_rows(test_run_id: int) -> dict[int, _Row]:
    return {
        pivot_id: _Row(
            pivot_id=pivot_id,
            input_hash=input_hash,
            classified_status=classified_status,
            classified_finished_at=classified_finished_at,
            source=source,
            updated_at=updated_at,
        )
        for (
            pivot_id,
            input_hash,
            classified_status,
            classified_finished_at,
            source,
            updated_at,
        ) in TriageCase.objects.filter(triage__test_run_id=test_run_id).values_list(
            "test_run_test_case_id",
            "input_hash",
            "classified_status",
            "classified_finished_at",
            "source",
            "updated_at",
        )
    }


def _named_cases(
    test_run_id: int,
    scope: ClassifyScope,
    refs: tuple[str, ...],
    cases: dict[int, _Case],
) -> set[int]:
    if scope != "cases":
        return set()
    if not refs:
        raise RunTriageError('Name the cases to re-check (scope "cases").')
    budget = max_named_refs()
    if len(refs) > budget:
        raise RunTriageError(
            f"Name at most {budget} cases per call; re-check the rest in "
            "further calls."
        )
    ref_map = run_ref_map(test_run_id)
    named: set[int] = set()
    unknown: list[str] = []
    not_analysable: list[str] = []
    for ref in refs:
        pivot_id = ref_map.get(normalize_ref(ref))
        if pivot_id is None:
            unknown.append(ref)
        elif pivot_id not in cases:
            not_analysable.append(ref)
        else:
            named.add(pivot_id)
    problems: list[str] = []
    if unknown:
        problems.append("not cases of this run: " + ", ".join(unknown[:20]))
    if not_analysable:
        problems.append(
            "not analysable (Passed, or no result yet): "
            + ", ".join(not_analysable[:20])
        )
    if problems:
        raise RunTriageError("Some references are wrong; " + "; ".join(problems))
    return named


def max_named_refs() -> int:
    """How many cases one ``scope="cases"`` call may name: what one call can
    classify."""
    return settings.RUN_TRIAGE_MAX_CALLS_PER_TOOL_CALL * settings.RUN_TRIAGE_BATCH_SIZE


def _turn_started_at(turn_id: int) -> datetime | None:
    return (
        SetupTurn.objects.filter(id=turn_id)
        .values_list("started_at", flat=True)
        .first()
    )


def _is_stale(row: _Row, case: _Case) -> bool:
    return (
        row.classified_status != case.status
        or row.classified_finished_at != case.finished_at
    )


def _candidates(
    cases: dict[int, _Case],
    rows: dict[int, _Row],
    scope: ClassifyScope,
    named: set[int],
    turn_started_at: datetime | None,
) -> list[int]:
    picked: list[int] = []
    for pivot_id, case in cases.items():
        if scope == "cases" and pivot_id not in named:
            continue
        row = rows.get(pivot_id)
        if row is None:
            picked.append(pivot_id)
            continue
        if (
            scope != "cases"
            and turn_started_at is not None
            and row.updated_at >= turn_started_at
        ):
            # Already written in this turn (a re-check of named cases always
            # runs: the agent names each case once).
            continue
        stale = _is_stale(row, case)
        if scope == "cases":
            take = pivot_id in named
        elif scope == "all":
            take = stale or row.source != TriageSource.PERSON
        else:
            take = stale or (row.source == TriageSource.AI and not row.input_hash)
        if take:
            picked.append(pivot_id)
    return picked


def _write_reused(
    *,
    turn_id: int,
    test_run_id: int,
    reused: Sequence[CaseDraft],
    gone: Sequence[int],
) -> int:
    """Refresh reused rows, drop rows of cases no longer analysable; the
    run's ``RunTriage`` id (created here on the first write)."""
    now = timezone.now()
    with transaction.atomic():
        require_running_turn(turn_id)
        triage, _ = RunTriage.objects.get_or_create(test_run_id=test_run_id)
        if gone:
            TriageCase.objects.filter(
                triage=triage, test_run_test_case_id__in=list(gone)
            ).delete()
        if reused:
            by_pivot = {draft.pivot_id: draft for draft in reused}
            objs = list(
                TriageCase.objects.filter(
                    triage=triage, test_run_test_case_id__in=list(by_pivot)
                ).only("id", "test_run_test_case_id", *_REUSE_FIELDS)
            )
            for obj in objs:
                draft = by_pivot[obj.test_run_test_case_id]
                obj.untested = draft.untested
                obj.classified_status = draft.status
                obj.classified_finished_at = draft.finished_at
                obj.updated_at = now
            TriageCase.objects.bulk_update(objs, list(_REUSE_FIELDS))
            triage.last_classified_at = now
            triage.prompt_version = TRIAGE_PROMPT_VERSION
            triage.save(update_fields=["last_classified_at", "prompt_version"])
        return triage.id


def _classify(
    run: TestRun,
    triage_id: int,
    turn_id: int,
    drafts: Sequence[CaseDraft],
    should_stop: Callable[[], bool],
    deadline: float | None,
) -> tuple[list[_Outcome], StopReason]:
    """Classify ``drafts`` round by round; (outcomes written, why it stopped
    early or "").

    A round is one wave of up to ``RUN_TRIAGE_PARALLEL`` calls, then one
    wave of the failed batches' halves (concurrently). No wave starts unless
    it can finish before ``deadline``; cases a skipped retry wave would have
    covered are not written and remain.
    """
    if not drafts:
        return [], ""
    size = settings.RUN_TRIAGE_BATCH_SIZE
    batches = [list(drafts[i : i + size]) for i in range(0, len(drafts), size)]
    parallel = settings.RUN_TRIAGE_PARALLEL
    call = _ClassifierCall(
        llm=build_llm_config(),
        context=run.project.project_prompt,
        taxonomy=triage_taxonomy(),
        timeout=float(settings.RUN_TRIAGE_CALL_TIMEOUT_SECONDS),
    )
    written: list[_Outcome] = []
    pool = ThreadPoolExecutor(max_workers=min(parallel, 2 * len(batches)))
    try:
        for start in range(0, len(batches), parallel):
            if should_stop():
                return written, "stop"
            if not fits_before(deadline, waves=1):
                return written, "deadline"
            round_batches = batches[start : start + parallel]
            outcomes, reason = _round(pool, call, round_batches, should_stop, deadline)
            if outcomes:
                _write_outcomes(turn_id, triage_id, outcomes)
                written.extend(outcomes)
            if reason:
                return written, reason
    except BaseException:
        # Includes Celery's SoftTimeLimitExceeded: never wait for the calls.
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return written, ""


@dataclass(frozen=True)
class _ClassifierCall:
    llm: LLMConfig
    context: str
    taxonomy: TriageTaxonomyText
    timeout: float

    def __call__(
        self, inputs: tuple[TriageCaseInput, ...]
    ) -> dict[str, CaseClassification] | AIServiceError:
        """Valid classifications by ref, or the AI error (returned, not raised).

        Runs in a worker thread: no ORM.
        """
        try:
            return _valid(
                run_case_classifier.classify_run_cases(
                    self.llm,
                    inputs,
                    application_context=self.context,
                    taxonomy=self.taxonomy,
                    timeout_seconds=self.timeout,
                )
            )
        except AIServiceError as exc:
            return exc


def _round(
    pool: ThreadPoolExecutor,
    call: _ClassifierCall,
    batches: list[list[CaseDraft]],
    should_stop: Callable[[], bool],
    deadline: float | None,
) -> tuple[list[_Outcome], StopReason]:
    """One wave of calls, then one concurrent wave of the failed batches'
    halves; the outcomes to write and why the retry wave was skipped."""
    inputs = [tuple(load_case_inputs(batch)) for batch in batches]
    first = [pool.submit(call, batch_inputs) for batch_inputs in inputs]
    answers: list[dict[str, CaseClassification]] = []
    failed: list[int] = []
    for index, future in enumerate(first):
        result = future.result()
        if isinstance(result, AIServiceError):
            answers.append({})
            failed.append(index)
        else:
            answers.append(result)
    halves = [(index, half) for index in failed for half in _halves(inputs[index])]
    reason: StopReason = ""
    if halves:
        waves = math.ceil(len(halves) / settings.RUN_TRIAGE_PARALLEL)
        if should_stop():
            reason = "stop"
        elif not fits_before(deadline, waves=waves):
            reason = "deadline"
        else:
            retried = [(index, pool.submit(call, half)) for index, half in halves]
            for index, future in retried:
                result = future.result()
                if not isinstance(result, AIServiceError):
                    answers[index].update(result)
    skipped = set(failed) if reason else set()
    return [
        _Outcome(draft=draft, classification=answers[index].get(draft.ref))
        for index, batch in enumerate(batches)
        if index not in skipped
        for draft in batch
    ], reason


def _halves(
    inputs: tuple[TriageCaseInput, ...],
) -> list[tuple[TriageCaseInput, ...]]:
    middle = max(1, len(inputs) // 2)
    return [half for half in (inputs[:middle], inputs[middle:]) if half]


def _valid(
    classifications: Sequence[CaseClassification],
) -> dict[str, CaseClassification]:
    return {
        item.ref: item
        for item in classifications
        if item.category in CATEGORY_ORDER
        and item.severity in SEVERITY_ORDER
        and item.finding.strip()
    }


def _write_outcomes(turn_id: int, triage_id: int, outcomes: Sequence[_Outcome]) -> None:
    now = timezone.now()
    by_pivot = {outcome.draft.pivot_id: outcome for outcome in outcomes}
    with transaction.atomic():
        require_running_turn(turn_id)
        existing = {
            obj.test_run_test_case_id: obj
            for obj in TriageCase.objects.filter(
                triage_id=triage_id, test_run_test_case_id__in=list(by_pivot)
            ).only("id", "test_run_test_case_id", *_ROW_FIELDS)
        }
        updated: list[TriageCase] = []
        created: list[TriageCase] = []
        for pivot_id, outcome in by_pivot.items():
            obj = existing.get(pivot_id)
            if obj is None:
                obj = TriageCase(triage_id=triage_id, test_run_test_case_id=pivot_id)
                created.append(obj)
            else:
                updated.append(obj)
            _apply(obj, outcome, now)
        if updated:
            TriageCase.objects.bulk_update(updated, list(_ROW_FIELDS))
        if created:
            TriageCase.objects.bulk_create(created)
        RunTriage.objects.filter(id=triage_id).update(
            last_classified_at=now, prompt_version=TRIAGE_PROMPT_VERSION, updated_at=now
        )


def _apply(obj: TriageCase, outcome: _Outcome, now: datetime) -> None:
    draft = outcome.draft
    item = outcome.classification
    if item is None:
        category = str(TriageCategory.OTHER)
        severity = str(TriageSeverity.MINOR)
        finding = NOT_CLASSIFIED_FINDING
        input_hash = ""
    else:
        category = item.category
        severity = item.severity
        finding = cut(one_line(item.finding), TRIAGE_FINDING_MAX_LENGTH)
        input_hash = draft.input_hash
    if obj.pk is not None and obj.category != category:
        obj.group = None
    obj.category = category
    obj.severity = severity
    obj.finding = finding
    obj.untested = draft.untested
    obj.input_hash = input_hash
    obj.classified_status = draft.status
    obj.classified_finished_at = draft.finished_at
    obj.source = str(TriageSource.AI)
    obj.corrected_by_label = ""
    obj.corrected_at = None
    obj.updated_at = now


def _summary(
    *,
    test_run_id: int,
    done: int,
    reused: int,
    failed: list[str],
    remaining: int,
    stopped: StopReason,
    nothing_to_do: bool,
) -> str:
    counts = category_counts_text(test_run_id)
    if nothing_to_do:
        return (
            "Nothing to classify: every analysable case is classified and up "
            f"to date.\n{counts}"
        )
    head = f"Classified {done} ({reused} reused, {len(failed)} failed)"
    if stopped == "deadline":
        tail = ". " + DEADLINE_STOP_TEXT.format(remaining=remaining)
    elif stopped:
        tail = f"; stopped on request, {remaining} remain."
    elif remaining > 0:
        tail = f"; {remaining} remain: call again."
    else:
        tail = "; none remain."
    lines = [head + tail]
    if failed:
        shown = ", ".join(failed[:_FAILED_REFS_SHOWN])
        more = (
            f" and {len(failed) - _FAILED_REFS_SHOWN} more"
            if len(failed) > _FAILED_REFS_SHOWN
            else ""
        )
        lines.append(
            f"The AI service did not answer for {plural(len(failed), 'case')} "
            f'({shown}{more}); they are stored as Other / Minor "Not '
            'classified" and a later pending call retries them.'
        )
    lines.append(counts)
    return "\n".join(lines)
