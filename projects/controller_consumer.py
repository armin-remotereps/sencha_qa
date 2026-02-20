from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from projects.controller_authenticator import (
    ControllerAuthenticator,
    HandshakeMessageBuilder,
)
from projects.controller_protocol import (
    ActionTypeRegistry,
    BaseActionEvent,
    BrowserClickActionEvent,
    BrowserDownloadActionEvent,
    BrowserGetElementsActionEvent,
    BrowserGetPageContentActionEvent,
    BrowserGetUrlActionEvent,
    BrowserHoverActionEvent,
    BrowserNavigateActionEvent,
    BrowserTakeScreenshotActionEvent,
    BrowserTypeActionEvent,
    ClickActionEvent,
    ControllerMessageBuilder,
    DragActionEvent,
    HoverActionEvent,
    KeyPressActionEvent,
    RunCommandActionEvent,
    ScreenshotActionEvent,
    TypeTextActionEvent,
)
from projects.controller_reply_tracker import ReplyTracker
from projects.models import Project
from projects.services import (
    abort_active_test_run_on_disconnect,
    broadcast_agent_status,
    mark_agent_disconnected,
)

logger = logging.getLogger(__name__)


class ControllerConsumer(AsyncWebsocketConsumer):
    _authenticated: bool
    _project: Project | None
    _reply_tracker: ReplyTracker
    _authenticator: ControllerAuthenticator
    _message_builder: ControllerMessageBuilder

    async def connect(self) -> None:
        self._authenticated = False
        self._project = None
        self._reply_tracker = ReplyTracker(self.channel_layer)
        self._authenticator = ControllerAuthenticator()
        self._message_builder = ControllerMessageBuilder(ActionTypeRegistry())
        await self.accept()

    async def receive(
        self, text_data: str | None = None, bytes_data: bytes | None = None
    ) -> None:
        if text_data is None:
            return

        try:
            data: dict[str, Any] = json.loads(text_data)
        except json.JSONDecodeError:
            await self._send_error("Invalid JSON")
            return

        if not self._authenticated:
            await self._handle_unauthenticated_message(data)
            return

        await self._route_authenticated_message(data)

    async def disconnect(self, close_code: int) -> None:
        if self._authenticated and self._project is not None:
            await self._cleanup_connection()

    async def _handle_unauthenticated_message(self, data: dict[str, Any]) -> None:
        msg_type: str = data.get("type", "")
        request_id: str = data.get("request_id", "")

        if msg_type == "handshake":
            await self._handle_handshake(data, request_id)
        else:
            await self._send_error(
                "Not authenticated. Send handshake first.", request_id
            )
            await self.close()

    async def _route_authenticated_message(self, data: dict[str, Any]) -> None:
        msg_type: str = data.get("type", "")
        request_id: str = data.get("request_id", "")

        if msg_type == "pong":
            return

        if msg_type == "error":
            logger.warning("Controller agent error: %s", data.get("message", ""))
            return

        handlers = {
            "action_result": self._reply_tracker.send_action_result,
            "screenshot_response": self._reply_tracker.send_screenshot_result,
            "command_result": self._reply_tracker.send_command_result,
            "browser_content_result": self._reply_tracker.send_browser_content_result,
        }

        handler = handlers.get(msg_type)
        if handler:
            await handler(request_id, data)

    async def _handle_handshake(self, data: dict[str, Any], request_id: str) -> None:
        api_key: str = data.get("api_key", "")
        system_info: dict[str, Any] = data.get("system_info", {})

        result = await self._authenticator.authenticate_handshake(api_key, system_info)

        if not result.success or result.project is None:
            status = "already_connected" if "already" in result.reason else "error"
            await self._send_handshake_ack(status, result.reason, request_id)
            await self.close()
            return

        await self._establish_authenticated_connection(result.project, request_id)

    async def _establish_authenticated_connection(
        self, project: Project, request_id: str
    ) -> None:
        self._authenticated = True
        self._project = project

        await self.channel_layer.group_add(
            self._controller_group_name(project.id), self.channel_name
        )
        await self._authenticator.broadcast_status(project)
        await self._send_handshake_ack(
            status="ok",
            message="Connected",
            request_id=request_id,
            project_id=str(project.id),
            project_name=project.name,
        )

    async def _cleanup_connection(self) -> None:
        if self._project is None:
            return

        await self.channel_layer.group_discard(
            self._controller_group_name(self._project.id), self.channel_name
        )
        await sync_to_async(mark_agent_disconnected)(self._project)
        await sync_to_async(broadcast_agent_status)(self._project)
        await sync_to_async(abort_active_test_run_on_disconnect)(self._project)

    async def _forward_action(self, event: BaseActionEvent) -> None:
        request_id: str = event.get("request_id") or str(uuid.uuid4())
        reply_channel: str = event.get("reply_channel", "")

        if reply_channel:
            self._reply_tracker.register_reply_channel(request_id, reply_channel)

        message = self._message_builder.build_action_message(event, request_id)
        await self.send(text_data=message)

    async def controller_click(self, event: ClickActionEvent) -> None:
        await self._forward_action(event)

    async def controller_hover(self, event: HoverActionEvent) -> None:
        await self._forward_action(event)

    async def controller_drag(self, event: DragActionEvent) -> None:
        await self._forward_action(event)

    async def controller_type_text(self, event: TypeTextActionEvent) -> None:
        await self._forward_action(event)

    async def controller_key_press(self, event: KeyPressActionEvent) -> None:
        await self._forward_action(event)

    async def controller_screenshot(self, event: ScreenshotActionEvent) -> None:
        await self._forward_action(event)

    async def controller_run_command(self, event: RunCommandActionEvent) -> None:
        await self._forward_action(event)

    async def controller_browser_navigate(
        self, event: BrowserNavigateActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_browser_click(self, event: BrowserClickActionEvent) -> None:
        await self._forward_action(event)

    async def controller_browser_type(self, event: BrowserTypeActionEvent) -> None:
        await self._forward_action(event)

    async def controller_browser_hover(self, event: BrowserHoverActionEvent) -> None:
        await self._forward_action(event)

    async def controller_browser_get_elements(
        self, event: BrowserGetElementsActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_browser_get_page_content(
        self, event: BrowserGetPageContentActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_browser_get_url(self, event: BrowserGetUrlActionEvent) -> None:
        await self._forward_action(event)

    async def controller_browser_take_screenshot(
        self, event: BrowserTakeScreenshotActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_browser_download(
        self, event: BrowserDownloadActionEvent
    ) -> None:
        await self._forward_action(event)

    async def _send_handshake_ack(
        self,
        status: str,
        message: str,
        request_id: str = "",
        project_id: str = "",
        project_name: str = "",
    ) -> None:
        ack_message = HandshakeMessageBuilder.build_handshake_ack(
            status, message, request_id, project_id, project_name
        )
        await self.send(text_data=ack_message)

    async def _send_error(self, message: str, request_id: str = "") -> None:
        error_message = HandshakeMessageBuilder.build_error(message, request_id)
        await self.send(text_data=error_message)

    @staticmethod
    def _controller_group_name(project_id: int) -> str:
        return f"controller_{project_id}"
