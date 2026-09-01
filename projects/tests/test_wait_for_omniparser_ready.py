from __future__ import annotations

from typing import Any
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from agents.types import AgentCancelledError
from projects.models import Project
from projects.services import ControllerActionError, _wait_for_omniparser_ready


def _make_project(
    agent_connected: bool = True,
    agent_omniparser_status: dict[str, Any] | None = None,
) -> Project:
    project = Project(id=1, name="P", api_key="k")
    project.agent_connected = agent_connected
    project.agent_omniparser_status = agent_omniparser_status or {}
    return project


class WaitForOmniparserReadyTests(SimpleTestCase):
    def test_returns_once_state_is_ready(self) -> None:
        project = _make_project(agent_omniparser_status={"state": "loading"})

        def _refresh(instance: Project) -> None:
            instance.agent_omniparser_status = {"state": "ready"}

        with (
            patch.object(Project, "refresh_from_db", _refresh),
            patch("projects.services.time.sleep"),
        ):
            _wait_for_omniparser_ready(project)  # should not raise

    def test_raises_controller_action_error_when_state_is_failed(self) -> None:
        project = _make_project(
            agent_omniparser_status={
                "state": "failed",
                "message": "weights missing",
                "phase": "weights",
                "device": "cpu",
                "weights_dir": "/weights",
            }
        )

        with (
            patch.object(Project, "refresh_from_db", lambda instance: None),
            patch("projects.services.time.sleep"),
        ):
            with self.assertRaises(ControllerActionError) as ctx:
                _wait_for_omniparser_ready(project)

        message = str(ctx.exception)
        self.assertIn("weights missing", message)
        self.assertIn("phase=weights", message)
        self.assertIn("device=cpu", message)
        self.assertIn("weights_dir=/weights", message)

    def test_raises_controller_action_error_when_disconnected(self) -> None:
        project = _make_project(agent_connected=False)

        with (
            patch.object(Project, "refresh_from_db", lambda instance: None),
            patch("projects.services.time.sleep"),
        ):
            with self.assertRaises(ControllerActionError):
                _wait_for_omniparser_ready(project)

    def test_raises_agent_cancelled_error_when_cancelled(self) -> None:
        project = _make_project(agent_omniparser_status={"state": "loading"})

        with (
            patch.object(Project, "refresh_from_db", lambda instance: None),
            patch("projects.services.time.sleep"),
        ):
            with self.assertRaises(AgentCancelledError):
                _wait_for_omniparser_ready(project, cancellation_check=lambda: True)

    @override_settings(CONTROLLER_OMNIPARSER_READY_TIMEOUT=0.05)
    def test_raises_timeout_error_naming_the_last_observed_state(self) -> None:
        project = _make_project(agent_omniparser_status={"state": "loading"})

        with (
            patch.object(Project, "refresh_from_db", lambda instance: None),
            patch("projects.services.time.sleep"),
        ):
            with self.assertRaises(TimeoutError) as ctx:
                _wait_for_omniparser_ready(project)

        message = str(ctx.exception)
        self.assertIn("loading", message)
        self.assertIn("0.05", message)
