from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Literal, Protocol, TypeAlias

from asgiref.sync import sync_to_async

from projects.models import Project
from projects.services import (
    broadcast_agent_status,
    check_controller_compatibility,
    claim_agent_connection,
    get_project_by_api_key,
)

HandshakeStatus: TypeAlias = Literal["ok", "error", "incompatible"]


class WebSocketSender(Protocol):
    async def send(self, text_data: str) -> None: ...
    async def close(self) -> None: ...


class AuthenticationResult:
    """Outcome of a single controller handshake attempt."""

    def __init__(
        self,
        status: HandshakeStatus,
        project: Project | None,
        reason: str = "",
    ) -> None:
        self.status = status
        self.project = project
        self.reason = reason

    @property
    def success(self) -> bool:
        return self.status == "ok"


class ControllerAuthenticator:
    """Validates a controller's handshake and establishes its connection."""

    async def authenticate_handshake(
        self,
        api_key: str,
        system_info: dict[str, Any],
        client_version: str,
        capabilities: Sequence[str],
    ) -> AuthenticationResult:
        """Run every handshake check in order and report the outcome.

        Compatibility is checked before the connection is ever marked
        established, so a stale controller is rejected up front instead of
        authenticating and then silently timing out on first use. A valid
        handshake never refuses on an existing connection: the caller claims
        the project with ``claim_connection`` (newest connection wins).
        """
        if not api_key:
            return AuthenticationResult("error", None, "Missing api_key")

        project = await sync_to_async(get_project_by_api_key)(api_key)
        if project is None:
            return AuthenticationResult("error", None, "Invalid API key")

        incompatibility_reason = check_controller_compatibility(
            client_version, capabilities
        )
        if incompatibility_reason is not None:
            return AuthenticationResult("incompatible", None, incompatibility_reason)

        return AuthenticationResult("ok", project)

    async def claim_connection(
        self,
        project: Project,
        channel_name: str,
        system_info: dict[str, Any],
        client_version: str,
        capabilities: Sequence[str],
    ) -> str:
        """Make ``channel_name`` the project's owner; return the replaced channel."""
        return await sync_to_async(claim_agent_connection)(
            project, channel_name, system_info, client_version, capabilities
        )

    async def broadcast_status(self, project: Project) -> None:
        await sync_to_async(broadcast_agent_status)(project)


class HandshakeMessageBuilder:
    @staticmethod
    def build_handshake_ack(
        status: HandshakeStatus,
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
    def build_error(
        message: str,
        request_id: str = "",
        code: str = "",
        details: str = "",
    ) -> str:
        return json.dumps(
            {
                "type": "error",
                "request_id": request_id,
                "message": message,
                "code": code,
                "details": details,
            }
        )
