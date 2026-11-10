"""The controller handshake: authenticate a Test Runner and claim its socket.

A runner authenticates with its own ``runner_token`` (``runners``); the
project Connection key no longer authenticates runners (it is the MCP
credential only). A handshake without a token or without USER_RUNNER_V1 is
a runner older than 0.25.0: it is answered ``incompatible`` with
``outdated_runner_message()``, which old runners treat as terminal.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Literal, Protocol, TypeAlias

from asgiref.sync import sync_to_async

from controller_client.protocol import ClientCapability
from projects.services import check_controller_compatibility
from runners.models import TestRunner
from runners.services.authenticate_runner import (
    authenticate_runner,
    outdated_runner_message,
)
from runners.services.runner_connection import RunnerClaim, claim_runner_connection
from runners.services.runner_status import broadcast_runner_status

HandshakeStatus: TypeAlias = Literal["ok", "error", "incompatible"]

# Answered to an unknown token, an archived runner or an inactive owner alike,
# so a probe learns nothing.
_INVALID_TOKEN = (
    "This Test Runner's token is not valid any more. Download the Test Runner "
    "again from its page on Punk Hazard."
)


class WebSocketSender(Protocol):
    async def send(self, text_data: str) -> None: ...
    async def close(self) -> None: ...


class AuthenticationResult:
    """Outcome of a single controller handshake attempt."""

    def __init__(
        self,
        status: HandshakeStatus,
        runner: TestRunner | None,
        reason: str = "",
    ) -> None:
        self.status = status
        self.runner = runner
        self.reason = reason

    @property
    def success(self) -> bool:
        return self.status == "ok"


class ControllerAuthenticator:
    """Validates a controller's handshake and establishes its connection."""

    async def authenticate_handshake(
        self,
        runner_token: str | None,
        client_version: str,
        capabilities: Sequence[str],
    ) -> AuthenticationResult:
        """Run every handshake check in order and report the outcome.

        An outdated runner (no token, or no USER_RUNNER_V1) is refused before
        any lookup. Compatibility is checked before the connection is ever
        marked established, so a stale controller is rejected up front
        instead of authenticating and then silently timing out on first use.
        A valid handshake never refuses on an existing connection: the caller
        claims the runner with ``claim_connection`` (newest connection wins).
        The token is never logged.
        """
        if (
            not runner_token
            or ClientCapability.USER_RUNNER_V1.value not in capabilities
        ):
            return AuthenticationResult("incompatible", None, outdated_runner_message())

        runner = await sync_to_async(authenticate_runner)(runner_token)
        if runner is None:
            return AuthenticationResult("error", None, _INVALID_TOKEN)

        incompatibility_reason = check_controller_compatibility(
            client_version, capabilities
        )
        if incompatibility_reason is not None:
            return AuthenticationResult("incompatible", None, incompatibility_reason)

        return AuthenticationResult("ok", runner)

    async def claim_connection(
        self,
        runner: TestRunner,
        channel_name: str,
        system_info: dict[str, Any],
        client_version: str,
        capabilities: Sequence[str],
    ) -> RunnerClaim:
        """Make ``channel_name`` the runner's owner; the refreshed runner and
        the replaced channel. Raises ``RunnerNotFoundError`` when the runner
        was archived since the token was checked."""
        return await sync_to_async(claim_runner_connection)(
            runner.runner_id, channel_name, system_info, client_version, capabilities
        )

    async def broadcast_status(self, runner: TestRunner) -> None:
        await sync_to_async(broadcast_runner_status)(runner)


class HandshakeMessageBuilder:
    @staticmethod
    def build_handshake_ack(
        status: HandshakeStatus,
        message: str,
        request_id: str = "",
        runner_id: str = "",
        runner_name: str = "",
    ) -> str:
        return json.dumps(
            {
                "type": "handshake_ack",
                "request_id": request_id,
                "status": status,
                "message": message,
                "runner_id": runner_id,
                "runner_name": runner_name,
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
