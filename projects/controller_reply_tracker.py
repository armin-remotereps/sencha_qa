from __future__ import annotations

import logging
from typing import Any

from channels.layers import BaseChannelLayer

logger = logging.getLogger(__name__)


class ReplyTracker:
    def __init__(self, channel_layer: BaseChannelLayer) -> None:
        self._channel_layer = channel_layer
        self._pending_replies: dict[str, str] = {}

    def register_reply_channel(self, request_id: str, reply_channel: str) -> None:
        self._pending_replies[request_id] = reply_channel

    def pop_reply_channel(self, request_id: str) -> str | None:
        return self._pending_replies.pop(request_id, None)

    def has_pending_reply(self, request_id: str) -> bool:
        return request_id in self._pending_replies

    async def send_action_result(self, request_id: str, data: dict[str, Any]) -> bool:
        reply_channel = self.pop_reply_channel(request_id)
        if not reply_channel:
            logger.warning(
                "Received action_result with unknown request_id: %s", request_id
            )
            return False

        await self._channel_layer.send(
            reply_channel,
            {
                "type": "action.result",
                "request_id": request_id,
                "success": data.get("success", False),
                "message": data.get("message", ""),
                "duration_ms": data.get("duration_ms", 0.0),
            },
        )
        return True

    async def send_screenshot_result(
        self, request_id: str, data: dict[str, Any]
    ) -> bool:
        reply_channel = self.pop_reply_channel(request_id)
        if not reply_channel:
            logger.warning(
                "Received screenshot_response with unknown request_id: %s", request_id
            )
            return False

        await self._channel_layer.send(
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
        return True

    async def send_command_result(self, request_id: str, data: dict[str, Any]) -> bool:
        reply_channel = self.pop_reply_channel(request_id)
        if not reply_channel:
            logger.warning(
                "Received command_result with unknown request_id: %s", request_id
            )
            return False

        await self._channel_layer.send(
            reply_channel,
            {
                "type": "command.result",
                "request_id": request_id,
                "success": data.get("success", False),
                "stdout": data.get("stdout", ""),
                "stderr": data.get("stderr", ""),
                "return_code": data.get("return_code", -1),
                "duration_ms": data.get("duration_ms", 0.0),
            },
        )
        return True

    async def send_browser_content_result(
        self, request_id: str, data: dict[str, Any]
    ) -> bool:
        reply_channel = self.pop_reply_channel(request_id)
        if not reply_channel:
            logger.warning(
                "Received browser_content_result with unknown request_id: %s",
                request_id,
            )
            return False

        await self._channel_layer.send(
            reply_channel,
            {
                "type": "browser_content.result",
                "request_id": request_id,
                "success": data.get("success", False),
                "content": data.get("content", ""),
                "duration_ms": data.get("duration_ms", 0.0),
            },
        )
        return True

    async def send_interactive_output(
        self, request_id: str, data: dict[str, Any]
    ) -> bool:
        reply_channel = self.pop_reply_channel(request_id)
        if not reply_channel:
            logger.warning(
                "Received interactive_output with unknown request_id: %s",
                request_id,
            )
            return False

        await self._channel_layer.send(
            reply_channel,
            {
                "type": "interactive.output",
                "request_id": request_id,
                "session_id": data.get("session_id", ""),
                "output": data.get("output", ""),
                "is_alive": data.get("is_alive", False),
                "exit_code": data.get("exit_code"),
                "duration_ms": data.get("duration_ms", 0.0),
            },
        )
        return True
