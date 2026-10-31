"""The main agent proposes one software change; a person approves it on a card.

Nothing is changed on the machine here: the action is stored with its NOTICE
card row, and ``action_approval_decision`` runs on the person's click. Each
call stores exactly one action (one card); there is no list form.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import partial

from django.db import IntegrityError, transaction

from machine_setup.exceptions import ActionApprovalInvalidError
from machine_setup.models import (
    APPROVAL_APPROACH_MAX_LENGTH,
    APPROVAL_KEY_MAX_LENGTH,
    APPROVAL_MAX_ATTEMPTS,
    APPROVAL_MAX_PENDING,
    APPROVAL_TARGET_MAX_LENGTH,
    APPROVAL_TITLE_MAX_LENGTH,
    APPROVAL_VERSION_MAX_LENGTH,
    OPEN_APPROVAL_STATUSES,
    ApprovalActionKind,
    ApprovalStatus,
    SetupActionApproval,
    SetupConversation,
    SetupConversationKind,
    SetupMessage,
    SetupMessageKind,
    SetupTurn,
    SetupTurnStatus,
)
from machine_setup.services.action_approval_events import broadcast_approval_card
from machine_setup.subtask_types import RUN_SETUP_TASK_TOOL

# Control characters (newlines included) are refused in one-line fields, so
# a title cannot pose as more lines of the server-built approval text.
_LINE_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# The approach may span lines; every other control character is refused.
_TEXT_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class ProposedAction:
    approval_id: int
    # For the agent's tool result.
    tool_text: str


def propose_action(
    *,
    project_id: int,
    conversation_id: int,
    turn_id: int | None,
    kind: str,
    title: str,
    target: str,
    version: str,
    requirement_key: str,
    artifact_key: str,
    approach: str,
) -> ProposedAction:
    """Validate and store one action, and post its card row.

    Locks the turn (when given) and then the conversation row, the order
    ``reset_conversation`` uses. Refuses an action already on an open card
    of this chat (same fingerprint) and an 11th pending card. An approved
    row whose attempts are used up is replaced, so the agent can propose it
    again. Raises ``ActionApprovalInvalidError`` with a message the agent
    can act on. The card is broadcast on commit.
    """
    action_kind = _kind(kind)
    title = _line("title", title, APPROVAL_TITLE_MAX_LENGTH, required=True)
    target = _line("target", target, APPROVAL_TARGET_MAX_LENGTH, required=True)
    version = _line("version", version, APPROVAL_VERSION_MAX_LENGTH, required=False)
    requirement_key = _line(
        "requirement_key", requirement_key, APPROVAL_KEY_MAX_LENGTH, required=False
    )
    artifact_key = _line(
        "artifact_key", artifact_key, APPROVAL_KEY_MAX_LENGTH, required=False
    )
    approach = _approach(approach)
    fingerprint = action_fingerprint(action_kind, target, version)
    try:
        with transaction.atomic():
            _check_turn_running(turn_id)
            _check_conversation(project_id, conversation_id)
            replaced_id = _replace_exhausted(conversation_id, fingerprint)
            _check_pending_count(conversation_id)
            approval = SetupActionApproval.objects.create(
                project_id=project_id,
                conversation_id=conversation_id,
                proposed_turn_id=turn_id,
                kind=action_kind,
                title=title,
                target=target,
                version=version,
                requirement_key=requirement_key,
                artifact_key=artifact_key,
                approach=approach,
                fingerprint=fingerprint,
            )
            SetupMessage.objects.create(
                conversation_id=conversation_id,
                turn_id=turn_id,
                kind=SetupMessageKind.NOTICE,
                text=f"Asked to approve #{approval.pk}: {title}",
                action_approval=approval,
            )
            if replaced_id is not None:
                transaction.on_commit(
                    partial(broadcast_approval_card, approval_id=replaced_id)
                )
            transaction.on_commit(
                partial(broadcast_approval_card, approval_id=approval.pk)
            )
    except IntegrityError as exc:
        raise ActionApprovalInvalidError(
            "This action was proposed at the same time by another call; it is "
            "already on a card in this chat."
        ) from exc
    return ProposedAction(
        approval_id=approval.pk,
        tool_text=(
            f"Stored as approval #{approval.pk}: {title}. Nothing is done yet: "
            "the person reviews the card and presses Approve. Once approved, "
            f"carry it out with {RUN_SETUP_TASK_TOOL}(approval_id={approval.pk})."
        ),
    )


def action_fingerprint(kind: str, target: str, version: str) -> str:
    """sha256 hex of kind + normalised target + version.

    The target is case-folded, its whitespace collapsed, backslashes read as
    slashes and trailing slashes dropped, so ``C:\\Tools\\`` and
    ``c:/tools`` are the same action.
    """
    normalised_target = _SPACES.sub(" ", target.strip()).replace("\\", "/")
    normalised_target = normalised_target.rstrip("/").casefold()
    normalised_version = _SPACES.sub(" ", version.strip()).casefold()
    raw = "\n".join((kind, normalised_target, normalised_version))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _kind(kind: str) -> ApprovalActionKind:
    value = kind.strip().lower()
    if value not in ApprovalActionKind.values:
        allowed = ", ".join(ApprovalActionKind.values)
        raise ActionApprovalInvalidError(f"kind must be one of: {allowed}.")
    return ApprovalActionKind(value)


def _line(name: str, value: str, limit: int, *, required: bool) -> str:
    value = value.strip()
    if required and not value:
        raise ActionApprovalInvalidError(f"{name} is required.")
    if len(value) > limit:
        raise ActionApprovalInvalidError(
            f"{name} is {len(value)} characters; keep it to {limit}."
        )
    if _LINE_CONTROL.search(value):
        raise ActionApprovalInvalidError(
            f"{name} must be one line without control characters."
        )
    return value


def _approach(value: str) -> str:
    value = value.strip()
    if not value:
        raise ActionApprovalInvalidError(
            "approach is required: say exactly how it will be done (the "
            "command or installer and its options)."
        )
    if len(value) > APPROVAL_APPROACH_MAX_LENGTH:
        raise ActionApprovalInvalidError(
            f"approach is {len(value)} characters; keep it to "
            f"{APPROVAL_APPROACH_MAX_LENGTH}."
        )
    if _TEXT_CONTROL.search(value):
        raise ActionApprovalInvalidError(
            "approach must not contain control characters."
        )
    return value


def _check_turn_running(turn_id: int | None) -> None:
    """The proposing turn must still be running (a closed turn writes nothing)."""
    if turn_id is None:
        return
    status = (
        SetupTurn.objects.select_for_update()
        .filter(pk=turn_id)
        .values_list("status", flat=True)
        .first()
    )
    if status != SetupTurnStatus.RUNNING:
        raise ActionApprovalInvalidError(
            "This turn has ended; the action was not stored."
        )


def _check_conversation(project_id: int, conversation_id: int) -> None:
    conversation = (
        SetupConversation.objects.select_for_update()
        .filter(pk=conversation_id, project_id=project_id)
        .only("id", "archived_at", "kind")
        .first()
    )
    if conversation is None or conversation.archived_at is not None:
        raise ActionApprovalInvalidError(
            "This chat was reset; the action was not stored."
        )
    if conversation.kind != SetupConversationKind.CHAT:
        raise ActionApprovalInvalidError(
            "Actions can be proposed in the main Machine setup chat only."
        )


def _replace_exhausted(conversation_id: int, fingerprint: str) -> int | None:
    """Refuse an open duplicate; supersede one whose attempts are used up.

    Returns the id of the superseded row, if any. One query (plus the update).
    """
    open_row = (
        SetupActionApproval.objects.filter(
            conversation_id=conversation_id,
            fingerprint=fingerprint,
            status__in=OPEN_APPROVAL_STATUSES,
        )
        .only("id", "status", "attempts")
        .first()
    )
    if open_row is None:
        return None
    if (
        open_row.status == ApprovalStatus.APPROVED
        and open_row.attempts >= APPROVAL_MAX_ATTEMPTS
    ):
        SetupActionApproval.objects.filter(pk=open_row.pk).update(
            status=ApprovalStatus.SUPERSEDED
        )
        return open_row.pk
    if open_row.status == ApprovalStatus.PENDING:
        raise ActionApprovalInvalidError(
            f"This action is already on card #{open_row.pk}, waiting for the "
            "person. Do not propose it again."
        )
    raise ActionApprovalInvalidError(
        f"This action is already approved as #{open_row.pk}. Carry it out with "
        f"{RUN_SETUP_TASK_TOOL}(approval_id={open_row.pk})."
    )


def _check_pending_count(conversation_id: int) -> None:
    pending = SetupActionApproval.objects.filter(
        conversation_id=conversation_id, status=ApprovalStatus.PENDING
    ).count()
    if pending >= APPROVAL_MAX_PENDING:
        raise ActionApprovalInvalidError(
            f"{pending} actions already wait for the person's decision (at most "
            f"{APPROVAL_MAX_PENDING}). Ask the person to approve or discard "
            "them before proposing more."
        )
