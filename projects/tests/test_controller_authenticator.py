from __future__ import annotations

from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from controller_client.protocol import CLIENT_VERSION, ClientCapability
from projects.controller_authenticator import ControllerAuthenticator
from projects.models import Project

_ALL_CAPABILITY_VALUES = tuple(c.value for c in ClientCapability)


class AuthenticateHandshakeTests(SimpleTestCase):
    def test_rejects_stale_client_before_marking_connected(self) -> None:
        authenticator = ControllerAuthenticator()
        project = Project(id=1, name="P", api_key="k")

        with (
            patch(
                "projects.controller_authenticator.get_project_by_api_key",
                return_value=project,
            ),
            patch(
                "projects.controller_authenticator.mark_agent_connected"
            ) as mock_mark_connected,
        ):
            result = async_to_sync(authenticator.authenticate_handshake)(
                "k", {}, "0.1.0", []
            )

        self.assertEqual(result.status, "incompatible")
        self.assertFalse(result.success)
        self.assertIsNone(result.project)
        mock_mark_connected.assert_not_called()

    def test_accepts_compatible_client_and_marks_connected(self) -> None:
        authenticator = ControllerAuthenticator()
        project = Project(id=1, name="P", api_key="k")
        system_info = {"os": "windows"}

        with (
            patch(
                "projects.controller_authenticator.get_project_by_api_key",
                return_value=project,
            ),
            patch(
                "projects.controller_authenticator.mark_agent_connected",
                return_value=True,
            ) as mock_mark_connected,
        ):
            result = async_to_sync(authenticator.authenticate_handshake)(
                "k", system_info, CLIENT_VERSION, _ALL_CAPABILITY_VALUES
            )

        self.assertEqual(result.status, "ok")
        self.assertTrue(result.success)
        self.assertIs(result.project, project)
        mock_mark_connected.assert_called_once_with(
            project, system_info, CLIENT_VERSION, _ALL_CAPABILITY_VALUES
        )

    def test_reports_already_connected_without_rejecting_capabilities(self) -> None:
        authenticator = ControllerAuthenticator()
        project = Project(id=1, name="P", api_key="k")

        with (
            patch(
                "projects.controller_authenticator.get_project_by_api_key",
                return_value=project,
            ),
            patch(
                "projects.controller_authenticator.mark_agent_connected",
                return_value=False,
            ),
        ):
            result = async_to_sync(authenticator.authenticate_handshake)(
                "k", {}, CLIENT_VERSION, _ALL_CAPABILITY_VALUES
            )

        self.assertEqual(result.status, "already_connected")
        self.assertIsNone(result.project)

    def test_rejects_missing_api_key(self) -> None:
        authenticator = ControllerAuthenticator()

        result = async_to_sync(authenticator.authenticate_handshake)(
            "", {}, CLIENT_VERSION, _ALL_CAPABILITY_VALUES
        )

        self.assertEqual(result.status, "error")

    def test_rejects_unknown_api_key(self) -> None:
        authenticator = ControllerAuthenticator()

        with patch(
            "projects.controller_authenticator.get_project_by_api_key",
            return_value=None,
        ):
            result = async_to_sync(authenticator.authenticate_handshake)(
                "bad-key", {}, CLIENT_VERSION, _ALL_CAPABILITY_VALUES
            )

        self.assertEqual(result.status, "error")
