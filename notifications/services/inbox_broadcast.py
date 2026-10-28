"""Tell a user's open tabs that their attention list changed (Channels).

Each user's sockets join ``attention_group(user_id)``. The event carries no
payload, so nothing user-specific or secret goes onto the channel layer: each
socket recounts its own user's attention items when it hears it.
"""

from __future__ import annotations

import logging

from asgiref.sync import async_to_sync
from channels.layers import BaseChannelLayer, get_channel_layer

from accounts.models import CustomUser

logger = logging.getLogger(__name__)

# Channels event type; a consumer handles it in ``attention_changed``.
ATTENTION_CHANGED_EVENT = "attention.changed"


def attention_group(user_id: int) -> str:
    """The Channels group every attention socket of ``user_id`` joins."""
    return f"attention_user_{user_id}"


def _send(layer: BaseChannelLayer, user_ids: list[int]) -> None:
    send = async_to_sync(layer.group_send)
    for user_id in dict.fromkeys(user_ids):
        send(attention_group(user_id), {"type": ATTENTION_CHANGED_EVENT})


def broadcast_inbox_changed(user_ids: list[int]) -> None:
    """Nudge the attention sockets of ``user_ids`` (duplicates sent once).

    Call it from ``transaction.on_commit`` after the change is saved. Never
    raises.
    """
    if not user_ids:
        return
    try:
        layer = get_channel_layer()
        if layer is None:
            return
        _send(layer, user_ids)
    except Exception:
        logger.exception("Attention broadcast failed for %d users", len(user_ids))


def broadcast_project_attention_changed(project_id: int) -> None:
    """Nudge the attention sockets of every active member of the project.

    One member id query, then one group send per member. Call it from
    ``transaction.on_commit``. Never raises.
    """
    try:
        layer = get_channel_layer()
        if layer is None:
            return
        user_ids = list(
            CustomUser.objects.filter(
                projects__id=project_id, is_active=True
            ).values_list("id", flat=True)
        )
        _send(layer, user_ids)
    except Exception:
        logger.exception("Attention broadcast failed for project %s", project_id)
