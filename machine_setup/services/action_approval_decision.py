"""A person's decision on an action card, and a sub-task's use of an approval.

``approve_action`` / ``discard_action`` run on the person's click.
``claim_approval_for_subtask``, ``settle_approval_after_subtask`` and
``approved_unused_note`` are the seams ``run_setup_task`` and ``run_turn``
call, so the server, not the model, decides which one action a helper may
carry out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import partial

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from machine_setup.exceptions import (
    ActionApprovalClosedError,
    ActionApprovalUnusableError,
    MachineSetupError,
    SetupRunActiveError,
)
from machine_setup.models import (
    APPROVAL_MAX_ATTEMPTS,
    ApprovalStatus,
    SetupActionApproval,
    SetupMessage,
    SetupMessageKind,
    SetupSubtaskResult,
)
from machine_setup.services.action_approval_events import (
    broadcast_approval_card,
    broadcast_notice,
)
from machine_setup.services.live_turn import resolve_live_turn
from machine_setup.services.queue_turn import broadcast_and_enqueue, create_user_turn
from projects.models import Project, TestRun, TestRunStatus
from projects.runner_update_state import (
    RUNNER_UPDATE_RUNNING_MESSAGE,
    runner_update_in_progress,
)

# Approved, unused actions listed in one turn's user note.
_NOTE_MAX_ROWS = 20

_CLOSED_MESSAGES: dict[str, str] = {
    ApprovalStatus.APPROVED: "This action was already approved.",
    ApprovalStatus.USED: "This action was already carried out.",
    ApprovalStatus.DISCARDED: "This action was discarded.",
    ApprovalStatus.SUPERSEDED: "This action was replaced by a newer card.",
}


@dataclass(frozen=True)
class ApprovalDecision:
    # The chat the card is in (to redirect to).
    conversation_id: int
    # A turn was queued for the approval; False when another turn was live.
    turn_queued: bool


@dataclass(frozen=True)
class ApprovedActionText:
    approval_id: int
    title: str
    # This sub-task's attempt, 1..APPROVAL_MAX_ATTEMPTS.
    attempt: int
    # Server-built APPROVED ACTION text for the executor prompt. Title,
    # target, version and keys are one line each (control characters are
    # refused at proposal); the approach is the agent's text, as approved.
    text: str


def approve_action(
    *, project: Project, user: CustomUser, approval_id: int
) -> ApprovalDecision:
    """Approve a pending action.

    Under the project row lock (the lock ``send_setup_message`` takes). With
    no live turn the action is approved, a USER row authored by ``user``
    ("<member> approved #N: <title>") is written and a turn is queued for
    it; every send gate applies except the machine READY one (Q3: the main
    chat may run offline). With a live turn the action is only approved and
    a NOTICE posted; the next turn's user note lists it.

    Raises ``ActionApprovalClosedError`` when the card is not pending or its
    chat was reset, ``SetupRunActiveError`` or ``MachineSetupError`` (runner
    update) when no turn may be queued; the card then stays pending.
    """
    outcome = _approve_locked(project=project, user=user, approval_id=approval_id)
    if isinstance(outcome, MachineSetupError):
        raise outcome
    return outcome


@transaction.atomic
def _approve_locked(
    *, project: Project, user: CustomUser, approval_id: int
) -> ApprovalDecision | MachineSetupError:
    locked = Project.objects.select_for_update().only("id").get(pk=project.pk)
    approval = _locked_pending(project=locked, approval_id=approval_id)
    if isinstance(approval, MachineSetupError):
        return approval
    now = timezone.now()
    live = resolve_live_turn(project_id=locked.pk, now=now)
    if live is None:
        refusal = approval_send_refusal(locked, now)
        if refusal is not None:
            # Returned, not raised: a stale turn closed above must still commit.
            return refusal
    _mark_decided(approval, ApprovalStatus.APPROVED, user, now)
    transaction.on_commit(partial(broadcast_approval_card, approval_id=approval.pk))
    member = user.get_full_name() or user.email
    if live is not None:
        notice = SetupMessage.objects.create(
            conversation_id=approval.conversation_id,
            kind=SetupMessageKind.NOTICE,
            text=(
                f"{member} approved #{approval.pk}: {approval.title}. The agent "
                "sees it with the next message."
            ),
        )
        transaction.on_commit(
            partial(broadcast_notice, project_id=locked.pk, row=notice)
        )
        return ApprovalDecision(
            conversation_id=approval.conversation_id, turn_queued=False
        )
    turn, message, _ = create_user_turn(
        conversation=approval.conversation,
        requested_by=user,
        text=f"{member} approved #{approval.pk}: {approval.title}",
    )
    transaction.on_commit(lambda: broadcast_and_enqueue(turn, message))
    return ApprovalDecision(conversation_id=approval.conversation_id, turn_queued=True)


@transaction.atomic
def discard_action(
    *, project: Project, user: CustomUser, approval_id: int
) -> ApprovalDecision:
    """Discard a pending action; nothing is done on the machine.

    Raises ``ActionApprovalClosedError`` when it is not pending.
    """
    approval = _locked_pending(project=project, approval_id=approval_id)
    if isinstance(approval, MachineSetupError):
        raise approval
    _mark_decided(approval, ApprovalStatus.DISCARDED, user, timezone.now())
    transaction.on_commit(partial(broadcast_approval_card, approval_id=approval.pk))
    return ApprovalDecision(conversation_id=approval.conversation_id, turn_queued=False)


def approval_send_refusal(project: Project, now: datetime) -> MachineSetupError | None:
    """``send_message._refusal`` without its live-turn and READY gates.

    The caller has already resolved the live turn (under the project row
    lock). Kept here so the approval path does not reach into a private
    function of ``send_message``.
    """
    if TestRun.objects.filter(project=project, status=TestRunStatus.STARTED).exists():
        return SetupRunActiveError()
    if runner_update_in_progress(project.pk, now):
        return MachineSetupError(RUNNER_UPDATE_RUNNING_MESSAGE)
    return None


@transaction.atomic
def claim_approval_for_subtask(
    *, conversation_id: int, approval_id: int
) -> ApprovedActionText:
    """Take one attempt of an approved action for a sub-task.

    The row is locked: it must be in ``conversation_id``, ``approved`` and
    have attempts left. ``attempts`` is incremented. Raises
    ``ActionApprovalUnusableError`` with a message safe for the agent.
    """
    approval = (
        SetupActionApproval.objects.select_for_update(of=("self",))
        .select_related("decided_by")
        .filter(pk=approval_id, conversation_id=conversation_id)
        .first()
    )
    if approval is None:
        raise ActionApprovalUnusableError(
            f"Approval #{approval_id} is not in this chat."
        )
    if approval.status == ApprovalStatus.PENDING:
        raise ActionApprovalUnusableError(
            f"#{approval_id} is not approved yet; wait for the person to press "
            "Approve on its card."
        )
    if approval.status != ApprovalStatus.APPROVED:
        raise ActionApprovalUnusableError(
            f"#{approval_id} cannot be used: it is "
            f"{approval.get_status_display().lower()}."
        )
    if approval.attempts >= APPROVAL_MAX_ATTEMPTS:
        raise ActionApprovalUnusableError(
            f"#{approval_id} was already tried {approval.attempts} times. Tell "
            "the person what went wrong; propose it again only if they agree."
        )
    approval.attempts += 1
    approval.save(update_fields=["attempts"])
    transaction.on_commit(partial(broadcast_approval_card, approval_id=approval.pk))
    return ApprovedActionText(
        approval_id=approval.pk,
        title=approval.title,
        attempt=approval.attempts,
        text=_approved_text(approval),
    )


def settle_approval_after_subtask(
    *, approval_id: int, outcome: SetupSubtaskResult
) -> None:
    """DONE marks the approval used; any other outcome leaves it reusable
    (while attempts remain). One UPDATE; the card is re-broadcast on commit."""
    if outcome != SetupSubtaskResult.DONE:
        return
    with transaction.atomic():
        updated = SetupActionApproval.objects.filter(
            pk=approval_id, status=ApprovalStatus.APPROVED
        ).update(status=ApprovalStatus.USED, used_at=timezone.now())
        if updated:
            transaction.on_commit(
                partial(broadcast_approval_card, approval_id=approval_id)
            )


def approved_unused_note(conversation_id: int) -> str:
    """The "Approved and not yet used" lines of a turn's user note; "" when none.

    One query, at most ``_NOTE_MAX_ROWS`` rows.
    """
    rows = list(
        SetupActionApproval.objects.filter(
            conversation_id=conversation_id,
            status=ApprovalStatus.APPROVED,
            attempts__lt=APPROVAL_MAX_ATTEMPTS,
        )
        .order_by("id")
        .values_list("id", "kind", "title", "attempts")[:_NOTE_MAX_ROWS]
    )
    if not rows:
        return ""
    lines = ["Approved and not yet used:"]
    for pk, kind, title, attempts in rows:
        retry = " (one attempt failed; one retry left)" if attempts else ""
        lines.append(f"- #{pk} [{kind}] {title}{retry}")
    return "\n".join(lines)


def _locked_pending(
    *, project: Project, approval_id: int
) -> SetupActionApproval | MachineSetupError:
    """The approval, locked, if it is in ``project``, pending and its chat live.

    Never loads the chat's ``llm_transcript``.
    """
    approval = (
        SetupActionApproval.objects.select_for_update(of=("self",))
        .select_related("conversation")
        .defer("conversation__llm_transcript")
        .filter(pk=approval_id, project=project)
        .first()
    )
    if approval is None:
        return ActionApprovalClosedError("This action was not found.")
    closed = _CLOSED_MESSAGES.get(approval.status)
    if closed is not None:
        return ActionApprovalClosedError(closed)
    if approval.conversation.archived_at is not None:
        return ActionApprovalClosedError("The chat was reset.")
    return approval


def _mark_decided(
    approval: SetupActionApproval,
    status: ApprovalStatus,
    user: CustomUser,
    now: datetime,
) -> None:
    approval.status = status
    approval.decided_by = user
    approval.decided_at = now
    approval.save(update_fields=["status", "decided_by", "decided_at"])


def _approved_text(approval: SetupActionApproval) -> str:
    decided_by = approval.decided_by
    approver = (decided_by.get_full_name() or decided_by.email) if decided_by else ""
    lines = [
        f"#{approval.pk} {approval.get_kind_display()}: {approval.title}",
        f"Target: {approval.target}",
    ]
    if approval.version:
        lines.append(f"Version: {approval.version}")
    if approval.requirement_key:
        lines.append(f"Requirement: {approval.requirement_key}")
    if approval.artifact_key:
        lines.append(f"Artifact: {approval.artifact_key}")
    if approver:
        lines.append(f"Approved by: {approver}")
    lines.append(f"Attempt: {approval.attempts} of {APPROVAL_MAX_ATTEMPTS}")
    lines.append("Approach (as the person saw and approved it):")
    lines.append(approval.approach)
    return "\n".join(lines)
