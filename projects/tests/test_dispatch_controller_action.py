from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from django.test import SimpleTestCase

from projects.services import ControllerActionError, _dispatch_controller_action


class _FakeChannelLayer:
    """A minimal stand-in for a channel layer, tracking every call made."""

    def __init__(self) -> None:
        self.new_channel_calls = 0
        self.group_sends: list[tuple[str, dict[str, Any]]] = []
        self._channel_counter = 0

    async def new_channel(self) -> str:
        self._channel_counter += 1
        self.new_channel_calls += 1
        return f"reply-channel-{self._channel_counter}"

    async def group_send(self, group: str, message: dict[str, Any]) -> None:
        self.group_sends.append((group, message))

    async def receive(self, channel: str) -> dict[str, Any]:
        return {"success": True}


class _HangingChannelLayer(_FakeChannelLayer):
    """A channel layer whose reply never arrives, to exercise timeouts."""

    async def receive(self, channel: str) -> dict[str, Any]:
        await asyncio.sleep(10)
        return {}


class DispatchControllerActionTests(SimpleTestCase):
    def test_uses_a_fresh_reply_channel_per_call(self) -> None:
        layer = _FakeChannelLayer()

        with patch("projects.services._get_channel_layer_or_raise", return_value=layer):
            _dispatch_controller_action(1, "controller.click", 5.0, x=1, y=2)
            _dispatch_controller_action(1, "controller.click", 5.0, x=1, y=2)

        self.assertEqual(layer.new_channel_calls, 2)
        first_channel = layer.group_sends[0][1]["reply_channel"]
        second_channel = layer.group_sends[1][1]["reply_channel"]
        self.assertNotEqual(first_channel, second_channel)

    def test_forwards_payload_keys_in_the_dispatched_event(self) -> None:
        layer = _FakeChannelLayer()

        with patch("projects.services._get_channel_layer_or_raise", return_value=layer):
            _dispatch_controller_action(
                7, "controller.click", 5.0, x=10, y=20, button="left"
            )

        group, message = layer.group_sends[0]
        self.assertEqual(group, "controller_7")
        self.assertEqual(message["type"], "controller.click")
        self.assertEqual(message["x"], 10)
        self.assertEqual(message["y"], 20)
        self.assertEqual(message["button"], "left")

    def test_timeout_raises_naming_the_event_type_and_duration(self) -> None:
        layer = _HangingChannelLayer()

        with patch("projects.services._get_channel_layer_or_raise", return_value=layer):
            with self.assertRaisesMessage(
                ControllerActionError,
                "Timed out waiting for controller reply to "
                "controller.find_element after 0.01s",
            ):
                _dispatch_controller_action(1, "controller.find_element", 0.01)
