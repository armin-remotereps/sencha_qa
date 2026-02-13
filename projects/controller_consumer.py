from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from controller_client.protocol import MessageType, serialize_message
from projects.models import Project
from projects.services import (
    broadcast_agent_status,
    get_project_by_api_key,
    mark_agent_connected,
    mark_agent_disconnected,
)

logger = logging.getLogger(__name__)

_ACTION_TYPE_MAP: dict[str, MessageType] = {
    "controller.click": MessageType.CLICK,
    "controller.hover": MessageType.HOVER,
    "controller.drag": MessageType.DRAG,
    "controller.type_text": MessageType.TYPE_TEXT,
    "controller.key_press": MessageType.KEY_PRESS,
    "controller.screenshot": MessageType.SCREENSHOT_REQUEST,
}

_ACTION_PAYLOAD_KEYS: dict[str, tuple[str, ...]] = {
    "controller.click": ("x", "y", "button"),
    "controller.hover": ("x", "y"),
    "controller.drag": ("start_x", "start_y", "end_x", "end_y", "button", "duration"),
    "controller.type_text": ("text", "interval"),
    "controller.key_press": ("keys",),
    "controller.screenshot": (),
}


class ControllerConsumer(AsyncWebsocketConsumer):  # type: ignore[misc]

    _authenticated: bool
    _project: Project | None
    _pending_replies: dict[str, str]

    async def connect(self) -> None:
        self._authenticated = False
        self._project = None
        self._pending_replies = {}
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

        msg_type: str = data.get("type", "")
        request_id: str = data.get("request_id", "")

        if not self._authenticated:
            if msg_type == "handshake":
                await self._handle_handshake(data, request_id)
            else:
                await self._send_error(
                    "Not authenticated. Send handshake first.", request_id
                )
                await self.close()
            return

        if msg_type == "pong":
            return
        elif msg_type == "error":
            logger.warning("Controller agent error: %s", data.get("message", ""))
        elif msg_type == "action_result":
            await self._handle_action_result(data, request_id)
        elif msg_type == "screenshot_response":
            await self._handle_screenshot_response(data, request_id)

    async def disconnect(self, close_code: int) -> None:
        if self._authenticated and self._project is not None:
            await self.channel_layer.group_discard(
                f"controller_{self._project.id}", self.channel_name
            )
            await sync_to_async(mark_agent_disconnected)(self._project)
            await sync_to_async(broadcast_agent_status)(self._project)

    # ------------------------------------------------------------------
    # Handshake
    # ------------------------------------------------------------------

    async def _handle_handshake(self, data: dict[str, Any], request_id: str) -> None:
        api_key: str = data.get("api_key", "")
        system_info: dict[str, Any] = data.get("system_info", {})

        if not api_key:
            await self._send_handshake_ack("error", "Missing api_key", request_id)
            await self.close()
            return

        project = await sync_to_async(get_project_by_api_key)(api_key)
        if project is None:
            await self._send_handshake_ack("error", "Invalid API key", request_id)
            await self.close()
            return

        connected = await sync_to_async(mark_agent_connected)(project, system_info)
        if not connected:
            await self._send_handshake_ack(
                "already_connected", "Agent already connected", request_id
            )
            await self.close()
            return

        self._authenticated = True
        self._project = project
        await self.channel_layer.group_add(
            f"controller_{project.id}", self.channel_name
        )
        await sync_to_async(broadcast_agent_status)(project)
        await self._send_handshake_ack(
            status="ok",
            message="Connected",
            request_id=request_id,
            project_id=str(project.id),
            project_name=project.name,
        )

    # ------------------------------------------------------------------
    # Channel layer action handlers
    # ------------------------------------------------------------------

    async def _forward_action(self, event: dict[str, Any]) -> None:
        event_type: str = event["type"]
        message_type = _ACTION_TYPE_MAP[event_type]
        payload_keys = _ACTION_PAYLOAD_KEYS[event_type]

        request_id: str = event.get("request_id") or str(uuid.uuid4())
        reply_channel: str = event.get("reply_channel", "")

        if reply_channel:
            self._pending_replies[request_id] = reply_channel

        payload_kwargs: dict[str, Any] = {
            key: event[key] for key in payload_keys if key in event
        }
        message = serialize_message(message_type, request_id, **payload_kwargs)
        await self.send(text_data=message)

    async def controller_click(self, event: dict[str, Any]) -> None:
        await self._forward_action(event)

    async def controller_hover(self, event: dict[str, Any]) -> None:
        await self._forward_action(event)

    async def controller_drag(self, event: dict[str, Any]) -> None:
        await self._forward_action(event)

    async def controller_type_text(self, event: dict[str, Any]) -> None:
        await self._forward_action(event)

    async def controller_key_press(self, event: dict[str, Any]) -> None:
        await self._forward_action(event)

    async def controller_screenshot(self, event: dict[str, Any]) -> None:
        await self._forward_action(event)

    # ------------------------------------------------------------------
    # Client response handlers
    # ------------------------------------------------------------------

    async def _handle_action_result(
        self, data: dict[str, Any], request_id: str
    ) -> None:
        reply_channel = self._pending_replies.pop(request_id, "")
        if not reply_channel:
            logger.warning(
                "Received action_result with unknown request_id: %s", request_id
            )
            return

        await self.channel_layer.send(
            reply_channel,
            {
                "type": "action.result",
                "request_id": request_id,
                "success": data.get("success", False),
                "message": data.get("message", ""),
                "duration_ms": data.get("duration_ms", 0.0),
            },
        )

    async def _handle_screenshot_response(
        self, data: dict[str, Any], request_id: str
    ) -> None:
        reply_channel = self._pending_replies.pop(request_id, "")
        if not reply_channel:
            logger.warning(
                "Received screenshot_response with unknown request_id: %s", request_id
            )
            return

        await self.channel_layer.send(
            reply_channel,
            {
                "type": "screenshot.result",
                "request_id": request_id,
                "success": data.get("success", False),
                "image_base64": data.get("image_base64", ""),
                "width": data.get("width", 0),
                "height": data.get("height", 0),
                "format": data.get("format", "png"),
            },
        )

    # ------------------------------------------------------------------
    # Outbound helpers
    # ------------------------------------------------------------------

    async def _send_handshake_ack(
        self,
        status: str,
        message: str,
        request_id: str = "",
        project_id: str = "",
        project_name: str = "",
    ) -> None:
        await self.send(
            text_data=json.dumps(
                {
                    "type": "handshake_ack",
                    "request_id": request_id,
                    "status": status,
                    "message": message,
                    "project_id": project_id,
                    "project_name": project_name,
                }
            )
        )

    async def _send_error(self, message: str, request_id: str = "") -> None:
        await self.send(
            text_data=json.dumps(
                {
                    "type": "error",
                    "request_id": request_id,
                    "message": message,
                }
            )
        )
