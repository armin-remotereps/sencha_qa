from __future__ import annotations

import json
from typing import Any

from asgiref.sync import sync_to_async

from accounts.models import CustomUser
from machine_setup.events import setup_group
from machine_setup.models import SetupConversation
from machine_setup.payloads import HelpThreadPayload
from machine_setup.presence_frame import parse_presence_frame
from machine_setup.services.chat_page import SetupPage, find_thread, get_setup_page
from machine_setup.services.help_thread_state import load_help_thread_payload
from machine_setup.services.viewer_presence import clear_viewing, update_viewing
from projects.consumers import AuthenticatedConsumer
from projects.models import Project
from projects.services import _agent_status_group
from visual_engine.services.pool_status import VISUAL_ENGINE_GROUP

# Conversation ids checked on this socket. A long-lived main-route socket
# sees a new id per reset, never more than a handful; past this many, unknown
# ids are dropped so a flood of made-up ids cannot cost a query per frame.
_MAX_CHECKED_CONVERSATIONS = 32


class MachineSetupConsumer(AuthenticatedConsumer):
    """Live machine setup chat: turn progress plus runner status on one socket.

    Joins the setup group (via the base class `group_name`), the
    project's agent status group and the global visual engine group. The only
    client frame is the presence heartbeat (``{"type": "presence", "visible":
    bool, "conversation_id": int}``); anything else is ignored.
    The thread route (``conversation_id`` in the URL) uses the same group; its
    ``current_state`` describes that thread (with ``help``, the help panel,
    for a help thread), and the client ignores rows of other conversations.
    """

    _project: Project
    _thread: SetupConversation | None
    _agent_group_name: str
    # Conversation id -> whether it is a non-archived thread of the project.
    _checked_conversations: dict[int, bool]
    # The conversation this socket last marked as viewed, cleared on close.
    _viewing_conversation_id: int | None = None

    async def _authorize(self) -> bool:
        project = await self._get_project()
        if project is None:
            return False
        conversation_id: int | None = self.scope["url_route"]["kwargs"].get(
            "conversation_id"
        )
        thread: SetupConversation | None = None
        if conversation_id is not None:
            thread = await sync_to_async(find_thread)(project, conversation_id)
            if thread is None:
                return False

        self._project = project
        self._thread = thread
        self.group_name = setup_group(project.id)
        self._agent_group_name = _agent_status_group(project.id)
        self._checked_conversations = {}
        return True

    async def _send_initial_state(self) -> None:
        # Join before reading state so no runner update is missed in between.
        await self.channel_layer.group_add(self._agent_group_name, self.channel_name)
        await self.channel_layer.group_add(VISUAL_ENGINE_GROUP, self.channel_name)
        project = self._project
        page: SetupPage = await sync_to_async(get_setup_page)(project, self._thread)
        help_state: HelpThreadPayload | None = None
        thread = self._thread
        if thread is not None and thread.help_request_id is not None:
            help_state = await sync_to_async(load_help_thread_payload)(
                thread.pk, thread.help_request_id
            )
        await self.send(
            text_data=json.dumps(
                {
                    "type": "current_state",
                    "conversation_id": page.conversation_id,
                    "kind": page.kind,
                    "other_thread_turn_live": page.other_thread_turn_live,
                    "messages": page.messages,
                    "hidden_message_count": page.hidden_message_count,
                    "turn": page.turn,
                    "can_send": page.can_send,
                    "send_blockers": list(page.send_blockers),
                    "finishing_previous_turn": page.finishing_previous_turn,
                    "finishing_conversation_id": page.finishing_conversation_id,
                    "agent_connected": project.agent_connected,
                    "visual_engine": page.visual_engine,
                    "help": help_state,
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
            await self.channel_layer.group_discard(
                VISUAL_ENGINE_GROUP, self.channel_name
            )
        if self._viewing_conversation_id is not None:
            await clear_viewing(
                self._viewing_conversation_id, self._user_id(), self.channel_name
            )
            self._viewing_conversation_id = None

    async def receive(
        self, text_data: str | None = None, bytes_data: bytes | None = None
    ) -> None:
        # Input goes through Django forms (PRG); the only frame is presence.
        # A bad frame is dropped, never answered by closing the socket.
        frame = parse_presence_frame(text_data)
        if frame is None or not await self._is_project_thread(frame.conversation_id):
            return
        self._viewing_conversation_id = await update_viewing(
            previous_id=self._viewing_conversation_id,
            conversation_id=frame.conversation_id,
            visible=frame.visible,
            user_id=self._user_id(),
            channel_name=self.channel_name,
        )

    async def _is_project_thread(self, conversation_id: int) -> bool:
        # Cached per socket so the 15 s heartbeat costs no query.
        checked = self._checked_conversations
        if conversation_id not in checked:
            if len(checked) >= _MAX_CHECKED_CONVERSATIONS:
                return False
            thread = await sync_to_async(find_thread)(self._project, conversation_id)
            checked[conversation_id] = thread is not None
        return checked[conversation_id]

    def _user_id(self) -> int:
        user: CustomUser = self.scope["user"]
        return user.pk

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

    async def machine_setup_help(self, event: dict[str, Any]) -> None:
        # Project-wide group: the client keeps only its own thread's panel.
        await self.send(text_data=json.dumps({"type": "help", "help": event["help"]}))

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
                }
            )
        )

    async def visual_engine_status(self, event: dict[str, Any]) -> None:
        await self.send(
            text_data=json.dumps(
                {"type": "visual_engine", "visual_engine": event["visual_engine"]}
            )
        )
