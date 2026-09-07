from __future__ import annotations

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from dashboard.services import get_dashboard_data
from projects.models import TestRunStatus, TestRunTestCaseStatus
from projects.tests.helpers import (
    add_case_to_run,
    finish_pivot,
    make_case,
    make_project,
    make_run,
    make_user,
)


class GetDashboardDataTests(TestCase):
    def test_empty_when_user_has_no_projects(self) -> None:
        user = make_user()

        data = get_dashboard_data(user)

        self.assertFalse(data.has_projects)
        self.assertEqual(data.projects, [])
        self.assertEqual(data.attention, [])
        self.assertEqual(data.activity, [])
        self.assertEqual(data.summary.runs_in_progress, 0)
        self.assertIsNone(data.summary.pass_rate_7d)

    def test_populated_dashboard_reports_projects_and_summary(self) -> None:
        user = make_user()
        project = make_project(user=user)
        make_case(project=project)

        data = get_dashboard_data(user)

        self.assertTrue(data.has_projects)
        self.assertEqual(len(data.projects), 1)
        self.assertEqual(data.projects[0].project.id, project.id)
        self.assertEqual(data.summary.never_run_cases, 1)

    def test_archived_projects_are_excluded(self) -> None:
        user = make_user()
        project = make_project(user=user)
        project.archived = True
        project.save()

        data = get_dashboard_data(user)

        self.assertFalse(data.has_projects)

    def test_scoped_to_the_requesting_user(self) -> None:
        user = make_user()
        other_user = make_user(email="other@example.com")
        make_project(user=other_user)

        data = get_dashboard_data(user)

        self.assertFalse(data.has_projects)

    def test_pass_rate_reflects_recently_finished_cases(self) -> None:
        user = make_user()
        project = make_project(user=user)
        case = make_case(project=project)
        test_run = make_run(project=project)
        test_run.status = TestRunStatus.DONE
        test_run.save()
        pivot = test_run.pivot_entries.create(
            test_case=case, status=TestRunTestCaseStatus.SUCCESS
        )
        pivot.finished_at = timezone.now()
        pivot.save(update_fields=["finished_at"])

        data = get_dashboard_data(user)

        self.assertEqual(data.summary.passed_7d, 1)
        self.assertEqual(data.summary.pass_rate_7d, 100)

    def test_failed_run_attention_item_reports_occurred_at(self) -> None:
        user = make_user()
        project = make_project(user=user)
        case = make_case(project=project)
        test_run = make_run(project=project)
        test_run.status = TestRunStatus.DONE
        test_run.save()
        pivot = add_case_to_run(test_run=test_run, test_case=case)
        finish_pivot(pivot, status=TestRunTestCaseStatus.FAILED)
        test_run.finished_at = pivot.finished_at
        test_run.save(update_fields=["finished_at"])

        data = get_dashboard_data(user)

        (item,) = data.attention
        self.assertEqual(item.kind, "failed_run")
        self.assertEqual(item.occurred_at, test_run.finished_at)

    def test_disconnected_machine_attention_item_reports_occurred_at(self) -> None:
        user = make_user()
        project = make_project(user=user)
        make_run(project=project)
        project.last_connected_at = timezone.now()
        project.save(update_fields=["last_connected_at"])

        data = get_dashboard_data(user)

        (item,) = data.attention
        self.assertEqual(item.kind, "disconnected_machine")
        self.assertEqual(item.occurred_at, project.last_connected_at)

    def test_run_finished_activity_item_reports_failed(self) -> None:
        user = make_user()
        project = make_project(user=user)
        case = make_case(project=project)
        test_run = make_run(project=project)
        test_run.status = TestRunStatus.DONE
        test_run.started_at = timezone.now()
        test_run.finished_at = timezone.now()
        test_run.save(update_fields=["status", "started_at", "finished_at"])
        pivot = add_case_to_run(test_run=test_run, test_case=case)
        finish_pivot(pivot, status=TestRunTestCaseStatus.FAILED)

        data = get_dashboard_data(user)

        run_finished_items = [
            item for item in data.activity if item.kind == "run_finished"
        ]
        (item,) = run_finished_items
        self.assertTrue(item.failed)

    def test_run_finished_activity_item_reports_not_failed_when_all_passed(
        self,
    ) -> None:
        user = make_user()
        project = make_project(user=user)
        case = make_case(project=project)
        test_run = make_run(project=project)
        test_run.status = TestRunStatus.DONE
        test_run.started_at = timezone.now()
        test_run.finished_at = timezone.now()
        test_run.save(update_fields=["status", "started_at", "finished_at"])
        pivot = add_case_to_run(test_run=test_run, test_case=case)
        finish_pivot(pivot, status=TestRunTestCaseStatus.SUCCESS)

        data = get_dashboard_data(user)

        run_finished_items = [
            item for item in data.activity if item.kind == "run_finished"
        ]
        (item,) = run_finished_items
        self.assertFalse(item.failed)

    def test_query_count_does_not_grow_with_project_count(self) -> None:
        """Guards against N+1s: the query count must stay flat as projects grow.

        This is a stronger regression guard than a single hard-coded
        `assertNumQueries` value, which would need updating (and could mask
        an N+1 that only appears at a different project count) every time
        an unrelated field is added to one of the batched queries.
        """
        user = make_user()
        for i in range(3):
            project = make_project(user=user, name=f"Small {i}")
            make_case(project=project)
        with CaptureQueriesContext(connection) as small_ctx:
            get_dashboard_data(user)

        for i in range(3, 15):
            project = make_project(user=user, name=f"Big {i}")
            make_case(project=project)
        with CaptureQueriesContext(connection) as big_ctx:
            get_dashboard_data(user)

        self.assertEqual(len(small_ctx.captured_queries), len(big_ctx.captured_queries))
        self.assertLessEqual(len(small_ctx.captured_queries), 20)
