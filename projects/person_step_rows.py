"""The worker's writes to a case's person steps (Human in the loop).

Opening the row a hand-off waits on, closing the rows the orchestrator
recorded a person attempt for, and naming the row in the checkpoint ``person``
key. Leaf module: models, ``agents.checkpoint`` and ``agents.os_shell`` only
(``projects.services`` imports it).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Literal

from django.db.models import Case, CharField, F, Q, Value, When
from django.utils import timezone

from agents.checkpoint import (
    checkpoint_from_json,
    read_checkpoint_person_step,
    with_person_step,
)
from agents.os_shell import get_os_name, shell_label
from agents.types import (
    CheckpointPersonStep,
    HandOffKind,
    OrchestratorCheckpoint,
    PersonHowToItem,
    PersonStepHandOff,
    SubTaskAttempt,
)
from projects.models import (
    OPEN_PERSON_STEP_STATUSES,
    PERSON_STEP_AGENT_TEXT_MAX_LENGTH,
    PERSON_STEP_COMMAND_MAX_LENGTH,
    PERSON_STEP_EXPECTED_MAX_LENGTH,
    PERSON_STEP_HOW_TO_COMMAND_MAX_LENGTH,
    PERSON_STEP_HOW_TO_MAX_ITEMS,
    PERSON_STEP_HOW_TO_TEXT_MAX_LENGTH,
    PERSON_STEP_INSTRUCTION_MAX_LENGTH,
    CasePersonStep,
    PersonStepClose,
    PersonStepOutcome,
    PersonStepStatus,
)

logger = logging.getLogger(__name__)

_FALLBACK_OBJECTION = "The agent could not confirm the reported result."
_HAND_OFF_KINDS: tuple[HandOffKind, ...] = ("interactive", "command", "restore")


def cap_text(text: str, limit: int) -> str:
    """``text`` stripped and cut to ``limit`` characters with an ellipsis."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def how_to_rows(how_to: Sequence[PersonHowToItem]) -> list[dict[str, str]]:
    """``CasePersonStep.how_to`` for a hand-off's items, capped again.

    Items without text are dropped; a command keeps its first line only.
    """
    rows: list[dict[str, str]] = []
    for item in how_to:
        text = cap_text(item.text, PERSON_STEP_HOW_TO_TEXT_MAX_LENGTH)
        if not text:
            continue
        command_lines = item.command.strip().splitlines()
        command = (
            cap_text(command_lines[0], PERSON_STEP_HOW_TO_COMMAND_MAX_LENGTH)
            if command_lines
            else ""
        )
        rows.append({"text": text, "command": command})
        if len(rows) >= PERSON_STEP_HOW_TO_MAX_ITEMS:
            break
    return rows


def stored_how_to(value: object) -> tuple[PersonHowToItem, ...]:
    """A row's stored ``how_to`` as items; malformed entries are skipped."""
    if not isinstance(value, list):
        return ()
    items: list[PersonHowToItem] = []
    for entry in value[:PERSON_STEP_HOW_TO_MAX_ITEMS]:
        if not isinstance(entry, dict):
            continue
        text = entry.get("text")
        command = entry.get("command", "")
        if not isinstance(text, str) or not text or not isinstance(command, str):
            continue
        items.append(PersonHowToItem(text=text, command=command))
    return tuple(items)


def stored_hand_off_kind(kind: str) -> HandOffKind:
    """A row's stored ``kind`` as a hand-off kind ("interactive" if unknown)."""
    for known in _HAND_OFF_KINDS:
        if kind == known:
            return known
    return "interactive"


def command_shell_label(kind: HandOffKind, system_info: object) -> str:
    """Where a person runs a command step (the agent's shell); blank otherwise."""
    if kind != "command":
        return ""
    info = system_info if isinstance(system_info, dict) else None
    return shell_label(get_os_name(info))


def _settle_row(
    pivot_id: int, person_step_id: int, attempt: SubTaskAttempt, now: datetime
) -> int:
    """Close one open row with its person attempt; the number of rows closed.

    Guarded by the open statuses, so an abort's ``cancelled`` (or any other
    close) is never overwritten. The close reason keeps one already recorded;
    otherwise reported different, overruled (the overrule sets ``closed_via``,
    and ``closed_by`` unless anonymous) or confirmed.
    """
    passed = attempt.result.status == "pass"
    return CasePersonStep.objects.filter(
        pk=person_step_id,
        test_run_test_case_id=pivot_id,
        status__in=OPEN_PERSON_STEP_STATUSES,
    ).update(
        status=PersonStepStatus.PASSED if passed else PersonStepStatus.FAILED,
        agent_summary=cap_text(
            attempt.result.summary or attempt.result.error or "",
            PERSON_STEP_AGENT_TEXT_MAX_LENGTH,
        ),
        closed_at=now,
        close_reason=Case(
            When(~Q(close_reason=""), then=F("close_reason")),
            When(
                outcome=PersonStepOutcome.DIFFERENT,
                then=Value(PersonStepClose.REPORTED_DIFFERENT.value),
            ),
            # Only an overrule sets an open row's ``closed_via``/``closed_by``; an
            # anonymous Test Runner overrule has no ``closed_by``.
            When(
                Q(closed_by__isnull=False) | ~Q(closed_via=""),
                then=Value(PersonStepClose.OVERRULED.value),
            ),
            default=Value(PersonStepClose.CONFIRMED.value),
            output_field=CharField(),
        ),
    )


def _person_attempts(
    checkpoint: OrchestratorCheckpoint,
) -> dict[int, SubTaskAttempt]:
    return {
        attempt.person_step_id: attempt
        for attempt in checkpoint.attempts
        if attempt.performed_by == "person" and attempt.person_step_id is not None
    }


# What the finalize / need-help backstop does with an open row that no person
# attempt of the checkpoint names (``settle_person_steps``): "keep" it,
# hand back one the checkpoint ``person`` key does not name ("unreferenced",
# a pause: a named row is still to be confirmed), or hand back any ("all",
# the case finished).
AbandonedRowPolicy = Literal["keep", "unreferenced", "all"]


def hand_back_person_step_row(pivot_id: int, person_step_id: int, now: datetime) -> int:
    """Close one open row as handed back to the agent; the number of rows closed.

    For a row no person attempt will ever settle: the run left Human in the
    loop while it was being confirmed and the agent performed the step. Status
    guarded like ``_settle_row``.
    """
    return CasePersonStep.objects.filter(
        pk=person_step_id,
        test_run_test_case_id=pivot_id,
        status__in=OPEN_PERSON_STEP_STATUSES,
    ).update(status=PersonStepStatus.HANDED_BACK, closed_at=now)


def settle_person_steps(
    pivot_id: int,
    checkpoint: OrchestratorCheckpoint | None,
    *,
    abandoned: AbandonedRowPolicy = "keep",
) -> None:
    """Close the open person step the checkpoint has a person attempt for.

    Backstop for ``build_person_step_settler`` (a save whose settle failed):
    called when a worker execution hands off or finishes. With ``abandoned``
    other than "keep" (finalize, need help) an open row with no person attempt
    is closed as handed back (see ``AbandonedRowPolicy``): the agent performed
    its step after the run left Human in the loop. "keep" (the hand-off path,
    where the reported row is reused as not confirmed) makes no query when the
    checkpoint has no person attempt; a case has at most one open row.
    """
    attempts = _person_attempts(checkpoint) if checkpoint is not None else {}
    if abandoned == "keep":
        if not attempts:
            return
        open_id = (
            CasePersonStep.objects.filter(
                test_run_test_case_id=pivot_id,
                pk__in=attempts,
                status__in=OPEN_PERSON_STEP_STATUSES,
            )
            .values_list("pk", flat=True)
            .first()
        )
    else:
        open_id = (
            CasePersonStep.objects.filter(
                test_run_test_case_id=pivot_id, status__in=OPEN_PERSON_STEP_STATUSES
            )
            .values_list("pk", flat=True)
            .first()
        )
    if open_id is None:
        return
    now = timezone.now()
    if open_id in attempts:
        _settle_row(pivot_id, open_id, attempts[open_id], now)
        return
    person = checkpoint.person if checkpoint is not None else None
    if abandoned == "all" or person is None or person.request_id != open_id:
        hand_back_person_step_row(pivot_id, open_id, now)


def _newest_attempt_is_person(checkpoint_data: dict[str, object]) -> bool:
    """A cheap look at the stored JSON before parsing the whole checkpoint."""
    attempts = checkpoint_data.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        return False
    newest = attempts[-1]
    return (
        isinstance(newest, dict)
        and newest.get("performed_by") == "person"
        and newest.get("person_step_id") is not None
    )


def _abandoned_by_agent(
    checkpoint_data: dict[str, object],
    row: tuple[int, int],
    baseline: int,
) -> bool:
    """Whether the saved checkpoint shows the agent did the open row's step.

    True when no attempt names the row and either an attempt recorded in this
    execution (index ``baseline`` on) is the agent's at the row's step, or the
    cursor moved past that step. Plain JSON reads, no parse.
    """
    row_id, step_num = row
    attempts = checkpoint_data.get("attempts")
    items = attempts if isinstance(attempts, list) else []
    if any(
        isinstance(item, dict) and item.get("person_step_id") == row_id
        for item in items
    ):
        return False
    if any(
        isinstance(item, dict)
        and item.get("performed_by") != "person"
        and item.get("step_num") == step_num
        for item in items[baseline:]
    ):
        return True
    next_step = checkpoint_data.get("next_step")
    return isinstance(next_step, int) and next_step > step_num


def _attempt_count(checkpoint_json: object) -> int:
    if not isinstance(checkpoint_json, dict):
        return 0
    attempts = checkpoint_json.get("attempts")
    return len(attempts) if isinstance(attempts, list) else 0


def build_person_step_settler(
    pivot_id: int, checkpoint_json: object
) -> Callable[[dict[str, object]], None]:
    """A checkpoint save hook closing a person step once its attempt is saved.

    The orchestrator records the confirmed (overruled, reported different)
    person attempt and saves the checkpoint; the row closes right then rather
    than when the worker exits, so an abort or a fault later in the case can
    no longer leave it open or turn it into ``cancelled``.

    The row open when the execution starts (``checkpoint_json`` is the stored
    checkpoint then) is read once: when a save shows the agent performed its
    step instead (the run left Human in the loop while the report was being
    confirmed, so the confirmation failure handed the step to the agent and
    nothing settles the row), it is closed as handed back. One query per
    execution; zero per save unless the newest attempt is a person attempt
    not settled yet or that open row is still tracked. Never raises: the
    hand-off and finalize backstop retries a failed settle.
    """
    settled: set[int] = set()
    baseline = _attempt_count(checkpoint_json)
    tracked: list[tuple[int, int]] = list(
        CasePersonStep.objects.filter(
            test_run_test_case_id=pivot_id, status__in=OPEN_PERSON_STEP_STATUSES
        ).values_list("pk", "step_num")[:1]
    )

    def _settle_saved(checkpoint_data: dict[str, object]) -> None:
        try:
            if _newest_attempt_is_person(checkpoint_data):
                checkpoint = checkpoint_from_json(checkpoint_data)
                if checkpoint is None or not checkpoint.attempts:
                    return
                attempt = checkpoint.attempts[-1]
                person_step_id = attempt.person_step_id
                if person_step_id is None or person_step_id in settled:
                    return
                _settle_row(pivot_id, person_step_id, attempt, timezone.now())
                settled.add(person_step_id)
                if tracked and tracked[0][0] == person_step_id:
                    tracked.clear()
            elif tracked and _abandoned_by_agent(checkpoint_data, tracked[0], baseline):
                row_id = tracked.pop()[0]
                hand_back_person_step_row(pivot_id, row_id, timezone.now())
        except Exception:
            logger.exception("Could not settle the person step of pivot %d", pivot_id)

    return _settle_saved


def _push_for_new_row(pivot_id: int, segment_started_at: datetime | None) -> bool:
    """Whether a new row starts a sequence the members must be told about.

    No push for a plain next step: the case's latest row passed in this
    execution segment. Any other history (no row yet, cancelled by an abort,
    handed back, failed, or passed in an earlier execution) pushes.
    """
    latest = (
        CasePersonStep.objects.filter(test_run_test_case_id=pivot_id)
        .order_by("-pk")
        .values_list("status", "closed_at")
        .first()
    )
    if latest is None:
        return True
    status, closed_at = latest
    if status != PersonStepStatus.PASSED or closed_at is None:
        return True
    return segment_started_at is not None and closed_at < segment_started_at


def open_person_step_row(
    pivot_id: int,
    hand_off: PersonStepHandOff,
    *,
    shell_label: str,
    segment_started_at: datetime | None,
    now: datetime,
) -> tuple[CasePersonStep, bool]:
    """The case's open person step for ``hand_off``, and whether to push.

    A ``reported`` row of the same step means the agent could not confirm the
    report: the row is reused (``not_confirmed``, next round, the objection)
    and pushed; it keeps its stored ``how_to``. A ``waiting`` or
    ``not_confirmed`` row of the same step is a re-entry and is kept as it
    is. Any other open row is cancelled and a new row opened, pushed unless
    it plainly follows a step passed in this execution segment
    (``segment_started_at``). The caller holds the run lock.
    """
    instruction = cap_text(hand_off.instruction, PERSON_STEP_INSTRUCTION_MAX_LENGTH)
    expected = cap_text(hand_off.expected_result, PERSON_STEP_EXPECTED_MAX_LENGTH)
    command = (
        cap_text(hand_off.command, PERSON_STEP_COMMAND_MAX_LENGTH)
        if hand_off.kind == "command"
        else ""
    )
    step_num = max(hand_off.step_num, 1)
    open_row = (
        CasePersonStep.objects.select_for_update()
        .filter(test_run_test_case_id=pivot_id, status__in=OPEN_PERSON_STEP_STATUSES)
        .first()
    )
    if (
        open_row is not None
        and open_row.step_num == step_num
        and open_row.kind == hand_off.kind
    ):
        if open_row.status != PersonStepStatus.REPORTED:
            return open_row, False
        open_row.status = PersonStepStatus.NOT_CONFIRMED
        open_row.round += 1
        open_row.agent_objection = cap_text(
            hand_off.objection or _FALLBACK_OBJECTION,
            PERSON_STEP_AGENT_TEXT_MAX_LENGTH,
        )
        open_row.instruction = instruction
        open_row.expected_result = expected
        open_row.command = command
        open_row.shell_label = shell_label
        open_row.case_steps = list(hand_off.case_steps)
        open_row.requested_at = now
        open_row.save(
            update_fields=[
                "status",
                "round",
                "agent_objection",
                "instruction",
                "expected_result",
                "command",
                "shell_label",
                "case_steps",
                "requested_at",
            ]
        )
        return open_row, True
    if open_row is not None:
        logger.warning(
            "Cancelling stale person step %d of pivot %d", open_row.pk, pivot_id
        )
        open_row.status = PersonStepStatus.CANCELLED
        open_row.closed_at = now
        open_row.save(update_fields=["status", "closed_at"])
    push = _push_for_new_row(pivot_id, segment_started_at)
    row = CasePersonStep.objects.create(
        test_run_test_case_id=pivot_id,
        step_num=step_num,
        kind=hand_off.kind,
        instruction=instruction,
        expected_result=expected,
        command=command,
        shell_label=shell_label,
        how_to=how_to_rows(hand_off.how_to),
        case_steps=list(hand_off.case_steps),
        agent_objection=cap_text(hand_off.objection, PERSON_STEP_AGENT_TEXT_MAX_LENGTH),
    )
    return row, push


def with_person_request_id(
    checkpoint_json: object,
    hand_off: PersonStepHandOff,
    request_id: int,
    now: datetime,
) -> dict[str, object]:
    """The stored checkpoint with the hand-off's ``person`` key naming its row."""
    person = read_checkpoint_person_step(checkpoint_json)
    if person is not None and person.hand_off.step_num == hand_off.step_num:
        person = replace(person, request_id=request_id)
    else:
        # The orchestrator writes the key before it stops; rebuilt if missing.
        person = CheckpointPersonStep(
            hand_off=hand_off, request_id=request_id, state="waiting", requested_at=now
        )
    return with_person_step(checkpoint_json, person)
