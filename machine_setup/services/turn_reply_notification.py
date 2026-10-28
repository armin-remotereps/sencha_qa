"""Tell the person who sent a setup turn that it ended while they were away.

The turn lands in their attention inbox (with a live nudge); if they opted in,
an ntfy push follows.
"""

from __future__ import annotations

import logging
import re

from django.db import transaction
from django.utils import timezone

from machine_setup.models import (
    SetupConversation,
    SetupMessage,
    SetupMessageKind,
    SetupTurn,
    SetupTurnEndReason,
    SetupTurnStatus,
)
from machine_setup.services.conversation_url import conversation_path
from machine_setup.services.viewer_presence import is_viewing
from notifications.models import MissedReplyOutcome
from notifications.services.enqueue_notification import enqueue_notification
from notifications.services.inbox_broadcast import broadcast_inbox_changed
from notifications.services.notification_message import (
    NotificationKind,
    NotificationMessage,
)
from notifications.services.ntfy_config import ntfy_configured
from notifications.services.recipients import recipient_user_ids
from notifications.services.record_missed_reply import record_missed_reply

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


def _title_and_body(
    turn: SetupTurn, notice: str
) -> tuple[MissedReplyOutcome, str, str]:
    project_name = turn.project.name
    if turn.status == SetupTurnStatus.FAILED:
        return (
            MissedReplyOutcome.FAILED,
            f"Turn failed · {project_name}",
            turn.error_message,
        )
    if turn.ended_reason in _LIMIT_REASONS:
        return (
            MissedReplyOutcome.PAUSED,
            f"Paused · {project_name}",
            notice or _NO_REPLY_TEXT,
        )
    return (
        MissedReplyOutcome.REPLY,
        f"Reply · {project_name}",
        _last_reply_text(turn) or _NO_REPLY_TEXT,
    )


def notify_turn_reply(turn: SetupTurn, *, notice: str) -> None:
    """Notify the person who sent ``turn`` that it ended, unless they are watching.

    Call it once, after the turn reached its end state; ``turn`` carries that
    state with ``project`` and ``conversation`` loaded, and ``notice`` is the
    notice row's text (shown for a step or time limit ending). Only answered
    or limit-ended (COMPLETED) and FAILED turns a person started notify;
    nothing for Stop, Reset, the Night auto-fix turn or an archived thread.
    Records a missed reply for the sender (live nudge on commit) whether or
    not ntfy is configured, then enqueues
    ``notifications.tasks.send_notification`` on commit if they opted in.
    Never raises.
    """
    try:
        user_id = turn.requested_by_id
        if turn.auto_fix or user_id is None:
            return
        if turn.status not in (SetupTurnStatus.COMPLETED, SetupTurnStatus.FAILED):
            return
        if is_viewing(turn.conversation_id, user_id):
            return
        # Fresh read: the thread may have been reset while the turn ran.
        if not SetupConversation.objects.filter(
            pk=turn.conversation_id, archived_at__isnull=True
        ).exists():
            return
        outcome, title, body = _title_and_body(turn, notice)
        preview = _preview(body)
        record_missed_reply(
            user_id=user_id,
            conversation_id=turn.conversation_id,
            outcome=outcome,
            preview=preview,
            occurred_at=timezone.now(),
        )
        transaction.on_commit(lambda: broadcast_inbox_changed([user_id]))
        if not ntfy_configured():
            return
        user_ids = recipient_user_ids(
            turn.project_id, NotificationKind.CHAT_REPLY, among=[user_id]
        )
        if not user_ids:
            return
        message = NotificationMessage.create(
            kind=NotificationKind.CHAT_REPLY,
            title=title,
            body=preview,
            path=conversation_path(turn.conversation),
        )
        transaction.on_commit(lambda: enqueue_notification(user_ids, message))
    except Exception:
        logger.exception("Chat reply notification failed for setup turn %s", turn.pk)
