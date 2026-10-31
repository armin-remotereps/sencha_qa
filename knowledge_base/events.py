"""Channel-layer broadcasts to a project's Knowledge base tab."""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction

logger = logging.getLogger(__name__)


class KnowledgeItemEventPayload(TypedDict):
    id: int
    parent_id: int | None
    status: str
    status_label: str
    warning: str
    error: str
    page_count: int


def knowledge_group(project_id: int) -> str:
    return f"knowledge_base_{project_id}"


def _send(project_id: int, event: dict[str, object]) -> None:
    layer: Any = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(knowledge_group(project_id), event)
    except Exception:
        # Runs after commit: a channel layer outage must not fail the request
        # or task whose change is already saved; the page shows it on reload.
        logger.warning(
            "Could not broadcast a knowledge base item update", exc_info=True
        )


def broadcast_item(project_id: int, payload: KnowledgeItemEventPayload) -> None:
    """Send the item's status to the tab once the current transaction commits."""
    transaction.on_commit(
        lambda: _send(project_id, {"type": "knowledge_base.item", "item": payload})
    )
