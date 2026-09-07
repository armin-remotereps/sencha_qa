from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import TestCase

from projects.models import TestRunStatus, TestRunTestCaseStatus
from projects.services import rerun_failed_cases
from projects.tests.helpers import make_case, make_project, make_run, make_user


@patch("projects.services.get_channel_layer", return_value=None)
class RerunFailedCasesTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)
        self.test_run = make_run(project=self.project, name="Original run")

    def test_raises_when_run_not_finished(
        self, mock_get_channel_layer: MagicMock
    ) -> None:
        with self.assertRaises(ValueError):
            rerun_failed_cases(self.test_run)

    def test_raises_when_no_failed_cases(
        self, mock_get_channel_layer: MagicMock
    ) -> None:
        self.test_run.status = TestRunStatus.DONE
        self.test_run.save()
        case = make_case(project=self.project)
        self.test_run.pivot_entries.create(
            test_case=case, status=TestRunTestCaseStatus.SUCCESS
        )

        with self.assertRaises(ValueError):
            rerun_failed_cases(self.test_run)

    def test_creates_new_run_with_only_failed_cases(
        self, mock_get_channel_layer: MagicMock
    ) -> None:
        self.test_run.status = TestRunStatus.DONE
        self.test_run.save()
        passed_case = make_case(project=self.project, title="Passed case")
        failed_case = make_case(project=self.project, title="Failed case")
        self.test_run.pivot_entries.create(
            test_case=passed_case, status=TestRunTestCaseStatus.SUCCESS
        )
        self.test_run.pivot_entries.create(
            test_case=failed_case, status=TestRunTestCaseStatus.FAILED
        )

        new_run = rerun_failed_cases(self.test_run)

        self.assertEqual(new_run.project_id, self.project.id)
        self.assertEqual(new_run.name, "Original run — failed rerun")
        case_ids = set(new_run.pivot_entries.values_list("test_case_id", flat=True))
        self.assertEqual(case_ids, {failed_case.id})

    def test_allows_rerun_from_cancelled_run(
        self, mock_get_channel_layer: MagicMock
    ) -> None:
        self.test_run.status = TestRunStatus.CANCELLED
        self.test_run.save()
        failed_case = make_case(project=self.project)
        self.test_run.pivot_entries.create(
            test_case=failed_case, status=TestRunTestCaseStatus.FAILED
        )

        new_run = rerun_failed_cases(self.test_run)

        self.assertEqual(new_run.pivot_entries.count(), 1)
