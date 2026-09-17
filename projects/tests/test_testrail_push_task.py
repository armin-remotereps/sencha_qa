from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from projects.tasks import push_test_run_results
from projects.testrail_client import TestRailError
from projects.testrail_results_services import TestRailPushOutcome
from projects.testrail_services import TestRailNotConfiguredError
from projects.tests.helpers import make_project, make_run, make_user

LINKS_BASE_URL = "https://example.com/"


class PushTestRunResultsTaskTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)
        self.test_run = make_run(project=self.project)

    def test_success_marks_push_succeeded_with_outcome(self) -> None:
        outcome = TestRailPushOutcome(
            pushed=2, unchanged=1, skipped_no_case_id=0, testrail_run_id=7
        )
        with patch(
            "projects.tasks.push_results_to_testrail", return_value=outcome
        ) as mock_push, patch(
            "projects.tasks.mark_testrail_push_succeeded"
        ) as mock_succeeded, patch(
            "projects.tasks.mark_testrail_push_failed"
        ) as mock_failed:
            push_test_run_results.apply(args=(self.test_run.id, LINKS_BASE_URL))

        mock_push.assert_called_once_with(self.test_run, links_base_url=LINKS_BASE_URL)
        mock_succeeded.assert_called_once_with(self.test_run, outcome)
        mock_failed.assert_not_called()

    def test_testrail_error_marks_push_failed_with_message(self) -> None:
        with patch(
            "projects.tasks.push_results_to_testrail",
            side_effect=TestRailError("boom", 400),
        ), patch(
            "projects.tasks.mark_testrail_push_succeeded"
        ) as mock_succeeded, patch(
            "projects.tasks.mark_testrail_push_failed"
        ) as mock_failed:
            push_test_run_results.apply(args=(self.test_run.id, LINKS_BASE_URL))

        mock_succeeded.assert_not_called()
        mock_failed.assert_called_once_with(self.test_run, "boom")

    def test_not_configured_error_marks_push_failed_with_message(self) -> None:
        with patch(
            "projects.tasks.push_results_to_testrail",
            side_effect=TestRailNotConfiguredError("not configured"),
        ), patch(
            "projects.tasks.mark_testrail_push_succeeded"
        ) as mock_succeeded, patch(
            "projects.tasks.mark_testrail_push_failed"
        ) as mock_failed:
            push_test_run_results.apply(args=(self.test_run.id, LINKS_BASE_URL))

        mock_succeeded.assert_not_called()
        mock_failed.assert_called_once_with(self.test_run, "not configured")

    def test_unexpected_error_uses_generic_message(self) -> None:
        with patch(
            "projects.tasks.push_results_to_testrail",
            side_effect=RuntimeError("boom"),
        ), patch(
            "projects.tasks.mark_testrail_push_succeeded"
        ) as mock_succeeded, patch(
            "projects.tasks.mark_testrail_push_failed"
        ) as mock_failed:
            push_test_run_results.apply(args=(self.test_run.id, LINKS_BASE_URL))

        mock_succeeded.assert_not_called()
        mock_failed.assert_called_once_with(
            self.test_run,
            "An error occurred while pushing results to TestRail.",
        )

    def test_missing_test_run_is_a_noop(self) -> None:
        with patch("projects.tasks.push_results_to_testrail") as mock_push, patch(
            "projects.tasks.mark_testrail_push_succeeded"
        ) as mock_succeeded, patch(
            "projects.tasks.mark_testrail_push_failed"
        ) as mock_failed:
            push_test_run_results.apply(args=(999_999, LINKS_BASE_URL))

        mock_push.assert_not_called()
        mock_succeeded.assert_not_called()
        mock_failed.assert_not_called()
