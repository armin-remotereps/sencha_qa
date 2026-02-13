from __future__ import annotations

import json
import logging
from typing import Any

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from projects.models import Project
from projects.services import (
    broadcast_agent_status,
    get_project_by_api_key,
    mark_agent_connected,
    mark_agent_disconnected,
)

logger = logging.getLogger(__name__)


class ControllerConsumer(AsyncWebsocketConsumer):  # type: ignore[misc]

    _authenticated: bool
    _project: Project | None

    async def connect(self) -> None:
        self._authenticated = False
        self._project = None
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

    async def disconnect(self, close_code: int) -> None:
        if self._authenticated and self._project is not None:
            await sync_to_async(mark_agent_disconnected)(self._project)
            await sync_to_async(broadcast_agent_status)(self._project)

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
        await sync_to_async(broadcast_agent_status)(project)
        await self._send_handshake_ack(
            status="ok",
            message="Connected",
            request_id=request_id,
            project_id=str(project.id),
            project_name=project.name,
        )

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
