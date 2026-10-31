"""Live updates of an action approval card (and its NOTICE rows) in the main chat."""

from __future__ import annotations

import logging

from machine_setup.events import broadcast_message
from machine_setup.models import SetupMessage, SetupMessageKind
from machine_setup.payloads import message_payload

logger = logging.getLogger(__name__)


def broadcast_approval_card(*, approval_id: int) -> None:
    """Re-broadcast the NOTICE row(s) that show the approval's card.

    One query. Run on commit; never raises (the page only misses a live
    update, and the next load shows the stored status).
    """
    try:
        rows = SetupMessage.objects.filter(
            action_approval_id=approval_id, kind=SetupMessageKind.NOTICE
        ).select_related("author", "action_approval")
        for row in rows:
            approval = row.action_approval
            if approval is None:
                continue
            broadcast_message(
                approval.project_id,
                message_payload(row, screenshots=[], attachments=[]),
            )
    except Exception:
        logger.exception("Action approval %s: card broadcast failed", approval_id)


def broadcast_notice(*, project_id: int, row: SetupMessage) -> None:
    """Broadcast a NOTICE row written without a card; never raises."""
    try:
        broadcast_message(project_id, message_payload(row, screenshots=[]))
    except Exception:
        logger.exception("Setup notice %s: broadcast failed", row.pk)
