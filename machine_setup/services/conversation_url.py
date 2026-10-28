"""Where a setup conversation opens in the app."""

from __future__ import annotations

from django.urls import reverse

from machine_setup.models import SetupConversation, SetupConversationKind


def conversation_path(conversation: SetupConversation) -> str:
    """Same-origin path of the conversation's page: the main chat or its thread."""
    if conversation.kind == SetupConversationKind.CHAT:
        return reverse(
            "machine_setup:chat", kwargs={"project_id": conversation.project_id}
        )
    return reverse(
        "machine_setup:thread",
        kwargs={
            "project_id": conversation.project_id,
            "conversation_id": conversation.pk,
        },
    )
