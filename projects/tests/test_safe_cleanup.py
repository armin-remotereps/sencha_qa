from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase

from projects.services import ActionResult, _safe_cleanup


def _result(success: bool, message: str) -> ActionResult:
    return ActionResult(success=success, message=message, duration_ms=1.0)


class SafeCleanupTests(SimpleTestCase):
    def test_logs_controller_summary_when_cleanup_succeeds(self) -> None:
        message = "browser closed; C:\\Users\\qa\\Downloads cleared (3 removed)"
        with (
            patch(
                "projects.services.controller_cleanup_environment",
                return_value=_result(True, message),
            ),
            self.assertLogs("projects.services", level="INFO") as logs,
        ):
            _safe_cleanup(7)

        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].levelname, "INFO")
        self.assertIn(message, logs.output[0])

    def test_warns_with_controller_summary_when_cleanup_reports_failures(self) -> None:
        message = "Downloads cleared (2 removed; 1 failed: locked (in use))"
        with (
            patch(
                "projects.services.controller_cleanup_environment",
                return_value=_result(False, message),
            ),
            self.assertLogs("projects.services", level="WARNING") as logs,
        ):
            _safe_cleanup(7)

        self.assertEqual(len(logs.records), 1)
        self.assertIn("project 7", logs.output[0])
        self.assertIn(message, logs.output[0])
