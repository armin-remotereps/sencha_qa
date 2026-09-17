from __future__ import annotations

from django.test import TestCase as DjangoTestCase

from projects.models import (
    TestRailPushStatus,
    TestRun,
    TestRunStatus,
    TestRunTestCaseStatus,
)
from projects.services import reset_test_run
from projects.tests.helpers import (
    add_case_to_run,
    finish_pivot,
    make_case,
    make_project,
    make_run,
    make_user,
)


class HasTestrailTargetTests(DjangoTestCase):
    """Behaviour of TestRun.has_testrail_target."""

    def test_true_when_project_and_suite_are_both_set(self) -> None:
        user = make_user()
        project = make_project(user=user)
        test_run = make_run(project=project)
        test_run.testrail_project_id = 1
        test_run.testrail_suite_id = 2

        self.assertTrue(test_run.has_testrail_target)

    def test_false_when_either_id_is_missing(self) -> None:
        user = make_user()
        project = make_project(user=user)
        test_run = make_run(project=project)

        self.assertFalse(test_run.has_testrail_target)

        test_run.testrail_project_id = 1
        self.assertFalse(test_run.has_testrail_target)

        test_run.testrail_project_id = None
        test_run.testrail_suite_id = 2
        self.assertFalse(test_run.has_testrail_target)


class ResetTestRunTestrailFieldsTests(DjangoTestCase):
    """reset_test_run clears push status while preserving TestRail identity."""

    def _make_finished_run_with_pushed_pivot(self) -> TestRun:
        user = make_user()
        project = make_project(user=user)
        test_run = make_run(project=project)
        test_case = make_case(project=project)
        pivot = add_case_to_run(test_run=test_run, test_case=test_case)
        finish_pivot(pivot, status=TestRunTestCaseStatus.SUCCESS)

        pivot.testrail_result_id = 555
        pivot.testrail_pushed_status_id = 1
        pivot.testrail_pushed_comment_hash = "deadbeef"
        pivot.save(
            update_fields=[
                "testrail_result_id",
                "testrail_pushed_status_id",
                "testrail_pushed_comment_hash",
                "updated_at",
            ]
        )

        test_run.testrail_run_id = 42
        test_run.testrail_project_id = 7
        test_run.testrail_suite_id = 9
        test_run.testrail_push_status = TestRailPushStatus.PUSHED
        test_run.testrail_push_summary = "12 results pushed"
        test_run.testrail_push_error = ""
        test_run.status = TestRunStatus.DONE
        test_run.save()
        return test_run

    def test_reset_clears_push_status_pushed_at_error_and_summary(self) -> None:
        test_run = self._make_finished_run_with_pushed_pivot()

        reset_test_run(test_run)
        test_run.refresh_from_db()

        self.assertEqual(test_run.testrail_push_status, TestRailPushStatus.NOT_PUSHED)
        self.assertIsNone(test_run.testrail_pushed_at)
        self.assertEqual(test_run.testrail_push_error, "")
        self.assertEqual(test_run.testrail_push_summary, "")

    def test_reset_keeps_testrail_run_id_and_pivot_tracking(self) -> None:
        test_run = self._make_finished_run_with_pushed_pivot()

        reset_test_run(test_run)
        test_run.refresh_from_db()
        pivot = test_run.pivot_entries.get()

        self.assertEqual(test_run.testrail_run_id, 42)
        self.assertEqual(pivot.testrail_result_id, 555)
        self.assertEqual(pivot.testrail_pushed_status_id, 1)
        self.assertEqual(pivot.testrail_pushed_comment_hash, "deadbeef")

    def test_reset_still_clears_ordinary_pivot_progress_fields(self) -> None:
        test_run = self._make_finished_run_with_pushed_pivot()

        reset_test_run(test_run)
        pivot = test_run.pivot_entries.get()

        self.assertEqual(pivot.status, TestRunTestCaseStatus.CREATED)
        self.assertEqual(pivot.result, "")
        self.assertIsNone(pivot.finished_at)
