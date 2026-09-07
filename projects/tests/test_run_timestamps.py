from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from agents.types import AgentResult, AgentStopReason
from projects.models import TestRunStatus, TestRunTestCaseStatus
from projects.services import (
    _finalize_pivot,
    _mark_pivot_in_progress,
    abort_test_run,
    reset_test_run,
    start_test_run,
)
from projects.tests.helpers import make_case, make_project, make_run, make_user


@patch("projects.services.get_channel_layer", return_value=None)
class StartTestRunTimestampTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)
        self.test_run = make_run(project=self.project)
        case = make_case(project=self.project)
        self.test_run.pivot_entries.create(test_case=case)

    @patch("projects.services.chain")
    def test_start_test_run_sets_started_at_and_clears_finished_at(
        self, mock_chain: MagicMock, mock_get_channel_layer: MagicMock
    ) -> None:
        mock_chain.return_value.apply_async.return_value = MagicMock(id="task-1")

        start_test_run(self.test_run)

        self.test_run.refresh_from_db()
        self.assertEqual(self.test_run.status, TestRunStatus.STARTED)
        self.assertIsNotNone(self.test_run.started_at)
        self.assertIsNone(self.test_run.finished_at)


@patch("projects.services.get_channel_layer", return_value=None)
class PivotTimestampTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)
        self.test_run = make_run(project=self.project)
        case = make_case(project=self.project)
        self.pivot = self.test_run.pivot_entries.create(test_case=case)

    def test_mark_pivot_in_progress_sets_started_at_on_pivot_and_run(
        self, mock_get_channel_layer: MagicMock
    ) -> None:
        _mark_pivot_in_progress(self.pivot)

        self.pivot.refresh_from_db()
        self.test_run.refresh_from_db()
        self.assertIsNotNone(self.pivot.started_at)
        self.assertEqual(self.test_run.status, TestRunStatus.STARTED)
        self.assertIsNotNone(self.test_run.started_at)

    def test_finalize_pivot_sets_finished_at(
        self, mock_get_channel_layer: MagicMock
    ) -> None:
        result = AgentResult(
            stop_reason=AgentStopReason.TASK_COMPLETE,
            iterations=1,
            messages=(),
        )

        _finalize_pivot(self.pivot, result)

        self.pivot.refresh_from_db()
        self.assertEqual(self.pivot.status, TestRunTestCaseStatus.SUCCESS)
        self.assertIsNotNone(self.pivot.finished_at)


@patch("projects.services.get_channel_layer", return_value=None)
class AbortAndResetTimestampTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)
        self.test_run = make_run(project=self.project)
        self.test_run.status = TestRunStatus.STARTED
        self.test_run.started_at = timezone.now()
        self.test_run.save()
        case = make_case(project=self.project)
        self.pivot = self.test_run.pivot_entries.create(
            test_case=case, status=TestRunTestCaseStatus.IN_PROGRESS
        )

    def test_abort_test_run_sets_finished_at(
        self, mock_get_channel_layer: MagicMock
    ) -> None:
        abort_test_run(self.test_run)

        self.test_run.refresh_from_db()
        self.assertEqual(self.test_run.status, TestRunStatus.CANCELLED)
        self.assertIsNotNone(self.test_run.finished_at)

    def test_reset_test_run_clears_all_four_timestamps(
        self, mock_get_channel_layer: MagicMock
    ) -> None:
        self.test_run.status = TestRunStatus.DONE
        self.test_run.finished_at = timezone.now()
        self.test_run.save()
        self.pivot.status = TestRunTestCaseStatus.SUCCESS
        self.pivot.started_at = timezone.now()
        self.pivot.finished_at = timezone.now()
        self.pivot.save()

        reset_test_run(self.test_run)

        self.test_run.refresh_from_db()
        self.pivot.refresh_from_db()
        self.assertIsNone(self.test_run.started_at)
        self.assertIsNone(self.test_run.finished_at)
        self.assertIsNone(self.pivot.started_at)
        self.assertIsNone(self.pivot.finished_at)
