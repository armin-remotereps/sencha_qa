"""Tell the person who sent a setup turn that it ended while they were away (ntfy)."""

from __future__ import annotations

import logging
import re

from django.db import transaction
from django.urls import reverse

from machine_setup.models import (
    SetupConversation,
    SetupConversationKind,
    SetupMessage,
    SetupMessageKind,
    SetupTurn,
    SetupTurnEndReason,
    SetupTurnStatus,
)
from machine_setup.services.viewer_presence import is_viewing
from notifications.services.enqueue_notification import enqueue_notification
from notifications.services.notification_message import (
    NotificationKind,
    NotificationMessage,
)
from notifications.services.ntfy_config import ntfy_configured
from notifications.services.recipients import recipient_user_ids

logger = logging.getLogger(__name__)

# The reply preview passes through the ntfy server: keep it short.
PREVIEW_MAX_LENGTH = 140
_ELLIPSIS = "…"
_NO_REPLY_TEXT = "The agent replied."
_LIMIT_REASONS = frozenset(
    {SetupTurnEndReason.ITERATION_LIMIT, SetupTurnEndReason.TIME_LIMIT}
)


# The reply is Markdown; a push shows plain text.
_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MARKDOWN_LINE_MARK = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]+|>[ \t]?|[-*+][ \t]+|\d+\.[ \t]+)", re.M
)
_MARKDOWN_TABLE_RULE = re.compile(r"^[ \t]*\|?[ \t:|-]+\|[ \t:|-]*$", re.M)
_MARKDOWN_BREAK = re.compile(r"```[^\n]*|\|")
_MARKDOWN_INLINE = re.compile(r"\*\*|`")


def _plain(text: str) -> str:
    # Only the start is shown; never scan a huge reply.
    text = text[: PREVIEW_MAX_LENGTH * 8]
    text = _MARKDOWN_LINK.sub(r"\1", text)
    text = _MARKDOWN_TABLE_RULE.sub("", text)
    text = _MARKDOWN_LINE_MARK.sub("", text)
    text = _MARKDOWN_BREAK.sub(" ", text)
    return _MARKDOWN_INLINE.sub("", text)


def _preview(text: str) -> str:
    collapsed = " ".join(_plain(text).split())
    if len(collapsed) <= PREVIEW_MAX_LENGTH:
        return collapsed
    return collapsed[: PREVIEW_MAX_LENGTH - len(_ELLIPSIS)].rstrip() + _ELLIPSIS


def _last_reply_text(turn: SetupTurn) -> str:
    text = (
        SetupMessage.objects.filter(
            turn=turn, kind=SetupMessageKind.ASSISTANT, parent__isnull=True
        )
        .order_by("-id")
        .values_list("text", flat=True)
        .first()
    )
    return text or ""


def _title_and_body(turn: SetupTurn, notice: str) -> tuple[str, str]:
    project_name = turn.project.name
    if turn.status == SetupTurnStatus.FAILED:
        return f"Turn failed · {project_name}", turn.error_message
    if turn.ended_reason in _LIMIT_REASONS:
        return f"Paused · {project_name}", notice or _NO_REPLY_TEXT
    return f"Reply · {project_name}", _last_reply_text(turn) or _NO_REPLY_TEXT


def _path(turn: SetupTurn) -> str:
    if turn.conversation.kind == SetupConversationKind.CHAT:
        return reverse("machine_setup:chat", kwargs={"project_id": turn.project_id})
    return reverse(
        "machine_setup:thread",
        kwargs={
            "project_id": turn.project_id,
            "conversation_id": turn.conversation_id,
        },
    )


def notify_turn_reply(turn: SetupTurn, *, notice: str) -> None:
    """Notify the person who sent ``turn`` that it ended, unless they are watching.

    Call it once, after the turn reached its end state; ``turn`` carries that
    state with ``project`` and ``conversation`` loaded, and ``notice`` is the
    notice row's text (shown for a step or time limit ending). Only answered
    or limit-ended (COMPLETED) and FAILED turns a person started notify;
    nothing for Stop, Reset, the Night auto-fix turn or an archived thread.
    Enqueues ``notifications.tasks.send_notification`` on commit and never
    raises.
    """
    try:
        user_id = turn.requested_by_id
        if turn.auto_fix or user_id is None:
            return
        if turn.status not in (SetupTurnStatus.COMPLETED, SetupTurnStatus.FAILED):
            return
        if not ntfy_configured():
            return
        if is_viewing(turn.conversation_id, user_id):
            return
        # Fresh read: the thread may have been reset while the turn ran.
        if not SetupConversation.objects.filter(
            pk=turn.conversation_id, archived_at__isnull=True
        ).exists():
            return
        user_ids = recipient_user_ids(
            turn.project_id, NotificationKind.CHAT_REPLY, among=[user_id]
        )
        if not user_ids:
            return
        title, body = _title_and_body(turn, notice)
        message = NotificationMessage.create(
            kind=NotificationKind.CHAT_REPLY,
            title=title,
            body=_preview(body),
            path=_path(turn),
        )
        transaction.on_commit(lambda: enqueue_notification(user_ids, message))
    except Exception:
        logger.exception("Chat reply notification failed for setup turn %s", turn.pk)
