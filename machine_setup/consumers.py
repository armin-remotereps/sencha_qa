from __future__ import annotations

import json
from typing import Any

from asgiref.sync import sync_to_async

from machine_setup.events import setup_group
from machine_setup.services.chat_page import SetupPage, get_setup_page
from projects.consumers import AuthenticatedConsumer
from projects.models import Project
from projects.services import _agent_status_group


class MachineSetupConsumer(AuthenticatedConsumer):
    """Live machine setup chat: turn progress plus runner status on one socket.

    Joins the setup group (via the base class `group_name`) and the
    project's agent status group. Receive-only: client frames are ignored.
    """

    _project: Project
    _agent_group_name: str

    async def _authorize(self) -> bool:
        project = await self._get_project()
        if project is None:
            return False

        self._project = project
        self.group_name = setup_group(project.id)
        self._agent_group_name = _agent_status_group(project.id)
        return True

    async def _send_initial_state(self) -> None:
        # Join before reading state so no runner update is missed in between.
        await self.channel_layer.group_add(self._agent_group_name, self.channel_name)
        project = self._project
        page: SetupPage = await sync_to_async(get_setup_page)(project)
        await self.send(
            text_data=json.dumps(
                {
                    "type": "current_state",
                    "conversation_id": page.conversation_id,
                    "messages": page.messages,
                    "hidden_message_count": page.hidden_message_count,
                    "turn": page.turn,
                    "can_send": page.can_send,
                    "send_blockers": list(page.send_blockers),
                    "finishing_previous_turn": page.finishing_previous_turn,
                    "finishing_conversation_id": page.finishing_conversation_id,
                    "agent_connected": project.agent_connected,
                    "agent_omniparser_status": project.agent_omniparser_status,
                }
            )
        )

    async def disconnect(self, close_code: int) -> None:
        await super().disconnect(close_code)
        if hasattr(self, "_agent_group_name"):
            await self.channel_layer.group_discard(
                self._agent_group_name,
                self.channel_name,
            )

    async def receive(
        self, text_data: str | None = None, bytes_data: bytes | None = None
    ) -> None:
        # Input goes through Django forms (PRG); the socket only pushes updates.
        return

    async def machine_setup_message(self, event: dict[str, Any]) -> None:
        await self.send(
            text_data=json.dumps({"type": "message", "message": event["message"]})
        )

    async def machine_setup_screenshot(self, event: dict[str, Any]) -> None:
        await self.send(
            text_data=json.dumps(
                {
                    "type": "screenshot",
                    "message_id": event["message_id"],
                    "screenshot": event["screenshot"],
                }
            )
        )

    async def machine_setup_turn(self, event: dict[str, Any]) -> None:
        await self.send(text_data=json.dumps({"type": "turn", "turn": event["turn"]}))

    async def machine_setup_reset(self, event: dict[str, Any]) -> None:
        await self.send(
            text_data=json.dumps({"type": "reset", "conversation_id": None})
        )

    async def agent_status(self, event: dict[str, Any]) -> None:
        await self.send(
            text_data=json.dumps(
                {
                    "type": "agent_status",
                    "agent_connected": event["agent_connected"],
                    "agent_omniparser_status": event["agent_omniparser_status"],
                }
            )
        )
