from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from projects.models import TestRunTestCaseStatus
from projects.services import list_test_cases_for_project
from projects.tests.helpers import make_case, make_project, make_run, make_user


class ListTestCasesForProjectLastRunAtTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)

    def test_is_none_when_never_run(self) -> None:
        case = make_case(project=self.project)

        page = list_test_cases_for_project(
            project=self.project, search=None, page=1, per_page=20
        )

        (result,) = page.object_list
        self.assertIsNone(result.last_run_at)

    def test_uses_the_latest_pivot_finished_at(self) -> None:
        case = make_case(project=self.project)
        run = make_run(project=self.project)
        pivot = run.pivot_entries.create(
            test_case=case, status=TestRunTestCaseStatus.SUCCESS
        )
        finished_at = timezone.now()
        pivot.finished_at = finished_at
        pivot.save(update_fields=["finished_at"])

        page = list_test_cases_for_project(
            project=self.project, search=None, page=1, per_page=20
        )

        (result,) = page.object_list
        self.assertEqual(result.last_run_at, finished_at)

    def test_uses_the_most_recent_pivot_across_multiple_runs(self) -> None:
        case = make_case(project=self.project)
        older_run = make_run(project=self.project)
        older_pivot = older_run.pivot_entries.create(test_case=case)
        older_pivot.finished_at = timezone.now() - timedelta(days=1)
        older_pivot.save(update_fields=["finished_at"])

        newer_run = make_run(project=self.project)
        newer_pivot = newer_run.pivot_entries.create(test_case=case)
        newer_finished_at = timezone.now()
        newer_pivot.finished_at = newer_finished_at
        newer_pivot.save(update_fields=["finished_at"])

        page = list_test_cases_for_project(
            project=self.project, search=None, page=1, per_page=20
        )

        (result,) = page.object_list
        self.assertEqual(result.last_run_at, newer_finished_at)
