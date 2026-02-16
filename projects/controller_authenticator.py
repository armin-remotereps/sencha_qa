from __future__ import annotations

import json
from typing import Any, Protocol

from asgiref.sync import sync_to_async
from projects.models import Project
from projects.services import (
    broadcast_agent_status,
    get_project_by_api_key,
    mark_agent_connected,
)


class WebSocketSender(Protocol):
    async def send(self, text_data: str) -> None: ...
    async def close(self) -> None: ...


class AuthenticationResult:
    def __init__(
        self, success: bool, project: Project | None, reason: str = ""
    ) -> None:
        self.success = success
        self.project = project
        self.reason = reason


class ControllerAuthenticator:
    async def authenticate_handshake(
        self, api_key: str, system_info: dict[str, Any]
    ) -> AuthenticationResult:
        if not api_key:
            return AuthenticationResult(False, None, "Missing api_key")

        project = await sync_to_async(get_project_by_api_key)(api_key)
        if project is None:
            return AuthenticationResult(False, None, "Invalid API key")

        connected = await sync_to_async(mark_agent_connected)(project, system_info)
        if not connected:
            return AuthenticationResult(False, None, "Agent already connected")

        return AuthenticationResult(True, project)

    async def broadcast_status(self, project: Project) -> None:
        await sync_to_async(broadcast_agent_status)(project)


class HandshakeMessageBuilder:
    @staticmethod
    def build_handshake_ack(
        status: str,
        message: str,
        request_id: str = "",
        project_id: str = "",
        project_name: str = "",
    ) -> str:
        return json.dumps(
            {
                "type": "handshake_ack",
                "request_id": request_id,
                "status": status,
                "message": message,
                "project_id": project_id,
                "project_name": project_name,
            }
        )

    @staticmethod
    def build_error(message: str, request_id: str = "") -> str:
        return json.dumps(
            {
                "type": "error",
                "request_id": request_id,
                "message": message,
            }
        )
