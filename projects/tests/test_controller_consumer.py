from __future__ import annotations

import asyncio
import json
import threading
from typing import Any
from unittest.mock import patch

from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.test import SimpleTestCase, override_settings

from controller_client.protocol import CLIENT_VERSION, ClientCapability, OmniParserState
from projects.controller_consumer import ControllerConsumer
from projects.models import Project

_ALL_CAPABILITY_VALUES = [c.value for c in ClientCapability]
_IN_MEMORY_CHANNEL_LAYERS = {
    "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}
}


def _handshake_message(client_version: str, capabilities: list[str]) -> str:
    return json.dumps(
        {
            "type": "handshake",
            "request_id": "req-handshake",
            "api_key": "k",
            "client_version": client_version,
            "capabilities": capabilities,
            "system_info": {"os": "windows"},
        }
    )


def _omniparser_status_message() -> str:
    return json.dumps(
        {
            "type": "omniparser_status",
            "request_id": "req-status",
            "state": OmniParserState.READY.value,
            "message": "Loaded on GPU",
            "device": "cuda",
            "weights_dir": "/weights",
            "phase": "ready",
            "load_seconds": 12.5,
        }
    )


def _find_element_result_message(request_id: str) -> str:
    return json.dumps(
        {
            "type": "find_element_result",
            "request_id": request_id,
            "success": True,
            "annotated_image_base64": "fake-base64",
            "elements": [
                {
                    "index": 0,
                    "type": "icon",
                    "content": "OK",
                    "bbox": {"x_min": 0, "y_min": 0, "x_max": 10, "y_max": 10},
                    "center_x": 5,
                    "center_y": 5,
                    "interactivity": True,
                }
            ],
            "image_width": 100,
            "image_height": 200,
        }
    )


def _find_element_error_message(request_id: str) -> str:
    return json.dumps(
        {
            "type": "error",
            "request_id": request_id,
            "code": "FIND_ELEMENT_FAILED",
            "message": "OmniParser weights not found at 'x'",
            "details": "phase=weights; device=cpu",
        }
    )


async def _connect_with_handshake(
    client_version: str, capabilities: list[str]
) -> tuple[WebsocketCommunicator, dict[str, Any]]:
    communicator = WebsocketCommunicator(
        ControllerConsumer.as_asgi(), "/ws/controller/"
    )
    await communicator.connect()
    await communicator.send_to(
        text_data=_handshake_message(client_version, capabilities)
    )
    ack: dict[str, Any] = await communicator.receive_json_from()
    return communicator, ack


@override_settings(CHANNEL_LAYERS=_IN_MEMORY_CHANNEL_LAYERS)
class ControllerConsumerHandshakeTests(SimpleTestCase):
    async def test_stale_handshake_is_rejected_and_socket_closed(self) -> None:
        project = Project(id=1, name="P", api_key="k")

        with (
            patch(
                "projects.controller_authenticator.get_project_by_api_key",
                return_value=project,
            ),
            patch(
                "projects.controller_authenticator.mark_agent_connected"
            ) as mock_mark_connected,
            patch("projects.controller_authenticator.broadcast_agent_status"),
        ):
            communicator, response = await _connect_with_handshake("0.1.0", [])

            self.assertEqual(response["type"], "handshake_ack")
            self.assertEqual(response["status"], "incompatible")
            self.assertIn("download", response["message"].lower())
            mock_mark_connected.assert_not_called()

            close_message = await communicator.receive_output()
            self.assertEqual(close_message["type"], "websocket.close")

            await communicator.disconnect()

    async def test_compatible_handshake_succeeds(self) -> None:
        project = Project(id=1, name="P", api_key="k")

        with (
            patch(
                "projects.controller_authenticator.get_project_by_api_key",
                return_value=project,
            ),
            patch(
                "projects.controller_authenticator.mark_agent_connected",
                return_value=True,
            ),
            patch("projects.controller_authenticator.broadcast_agent_status"),
            patch("projects.controller_consumer.mark_agent_disconnected"),
            patch("projects.controller_consumer.broadcast_agent_status"),
            patch("projects.controller_consumer.abort_active_test_run_on_disconnect"),
        ):
            communicator, response = await _connect_with_handshake(
                CLIENT_VERSION, _ALL_CAPABILITY_VALUES
            )

            self.assertEqual(response["type"], "handshake_ack")
            self.assertEqual(response["status"], "ok")

            await communicator.disconnect()

    async def test_omniparser_status_reaches_service_after_handshake(self) -> None:
        project = Project(id=1, name="P", api_key="k")

        # update_agent_omniparser_status runs on a worker thread (via
        # sync_to_async), so a plain post-await assertion would race it. A
        # threading.Event gives a deterministic synchronization point.
        ready_event = threading.Event()

        def _record_call(_project: Project, _payload: Any) -> None:
            ready_event.set()

        with (
            patch(
                "projects.controller_authenticator.get_project_by_api_key",
                return_value=project,
            ),
            patch(
                "projects.controller_authenticator.mark_agent_connected",
                return_value=True,
            ),
            patch("projects.controller_authenticator.broadcast_agent_status"),
            patch("projects.controller_consumer.mark_agent_disconnected"),
            patch("projects.controller_consumer.broadcast_agent_status"),
            patch("projects.controller_consumer.abort_active_test_run_on_disconnect"),
            patch(
                "projects.controller_consumer.update_agent_omniparser_status",
                side_effect=_record_call,
            ) as mock_update_status,
        ):
            communicator, _ = await _connect_with_handshake(
                CLIENT_VERSION, _ALL_CAPABILITY_VALUES
            )

            await communicator.send_to(text_data=_omniparser_status_message())
            await asyncio.to_thread(ready_event.wait, 2)

            mock_update_status.assert_called_once()
            called_project, called_payload = mock_update_status.call_args.args
            self.assertIs(called_project, project)
            self.assertEqual(called_payload.state, OmniParserState.READY)
            self.assertEqual(called_payload.device, "cuda")

            await communicator.disconnect()


@override_settings(CHANNEL_LAYERS=_IN_MEMORY_CHANNEL_LAYERS)
class ControllerConsumerFindElementBoundaryTests(SimpleTestCase):
    async def test_find_element_request_and_success_reply_cross_the_boundary(
        self,
    ) -> None:
        project = Project(id=1, name="P", api_key="k")

        with (
            patch(
                "projects.controller_authenticator.get_project_by_api_key",
                return_value=project,
            ),
            patch(
                "projects.controller_authenticator.mark_agent_connected",
                return_value=True,
            ),
            patch("projects.controller_authenticator.broadcast_agent_status"),
            patch("projects.controller_consumer.mark_agent_disconnected"),
            patch("projects.controller_consumer.broadcast_agent_status"),
            patch("projects.controller_consumer.abort_active_test_run_on_disconnect"),
        ):
            communicator, _ = await _connect_with_handshake(
                CLIENT_VERSION, _ALL_CAPABILITY_VALUES
            )
            channel_layer = get_channel_layer()
            assert channel_layer is not None
            reply_channel = await channel_layer.new_channel()

            await channel_layer.group_send(
                "controller_1",
                {
                    "type": "controller.find_element",
                    "request_id": "req-1",
                    "reply_channel": reply_channel,
                    "box_threshold": None,
                    "iou_threshold": None,
                },
            )

            forwarded = await communicator.receive_json_from()
            self.assertEqual(forwarded["type"], "find_element")
            self.assertEqual(forwarded["request_id"], "req-1")
            self.assertIsNone(forwarded["box_threshold"])
            self.assertIsNone(forwarded["iou_threshold"])

            await communicator.send_to(text_data=_find_element_result_message("req-1"))

            reply = await channel_layer.receive(reply_channel)
            self.assertEqual(reply["type"], "find_element.result")
            self.assertTrue(reply["success"])
            self.assertEqual(len(reply["elements"]), 1)
            self.assertEqual(reply["elements"][0]["content"], "OK")

            await communicator.disconnect()

    async def test_find_element_error_reply_carries_message_and_details(self) -> None:
        project = Project(id=1, name="P", api_key="k")

        with (
            patch(
                "projects.controller_authenticator.get_project_by_api_key",
                return_value=project,
            ),
            patch(
                "projects.controller_authenticator.mark_agent_connected",
                return_value=True,
            ),
            patch("projects.controller_authenticator.broadcast_agent_status"),
            patch("projects.controller_consumer.mark_agent_disconnected"),
            patch("projects.controller_consumer.broadcast_agent_status"),
            patch("projects.controller_consumer.abort_active_test_run_on_disconnect"),
        ):
            communicator, _ = await _connect_with_handshake(
                CLIENT_VERSION, _ALL_CAPABILITY_VALUES
            )
            channel_layer = get_channel_layer()
            assert channel_layer is not None
            reply_channel = await channel_layer.new_channel()

            await channel_layer.group_send(
                "controller_1",
                {
                    "type": "controller.find_element",
                    "request_id": "req-2",
                    "reply_channel": reply_channel,
                    "box_threshold": None,
                    "iou_threshold": None,
                },
            )
            await communicator.receive_json_from()

            await communicator.send_to(text_data=_find_element_error_message("req-2"))

            reply = await channel_layer.receive(reply_channel)
            self.assertEqual(reply["type"], "error.result")
            self.assertEqual(reply["message"], "OmniParser weights not found at 'x'")
            self.assertEqual(reply["details"], "phase=weights; device=cpu")

            await communicator.disconnect()
