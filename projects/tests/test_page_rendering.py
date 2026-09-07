from __future__ import annotations

import re

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from projects.models import (
    ApplicationPlatform,
    TestCaseUpload,
    TestRunStatus,
    TestRunTestCaseStatus,
    UploadStatus,
)
from projects.tests.helpers import (
    add_case_to_run,
    finish_pivot,
    make_case,
    make_project,
    make_run,
    make_user,
)


def _count_post_forms(content: bytes) -> int:
    return len(re.findall(rb'<form[^>]*method="post"', content, re.I))


def _count_csrf_tokens(content: bytes) -> int:
    return content.count(b'name="csrfmiddlewaretoken"')


# Smoke coverage for every server-rendered project page: templates are only
# exercised at render time, so a wrong `{% url %}` argument or a missing include
# variable would otherwise slip past the unit tests for services and views.


class ProjectPageRenderingTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.other_user = make_user(email="other@example.com")
        self.project = make_project(user=self.user, name="Acme Billing")
        self.project.application_url = "https://billing.example.test"
        self.project.application_platform = ApplicationPlatform.WEB
        self.project.project_prompt = "Staging tenant seeded nightly."
        self.project.save()

        self.passed_case = make_case(project=self.project, title="Sign in")
        self.failed_case = make_case(project=self.project, title="Export PDF")
        self.never_run_case = make_case(project=self.project, title="Refund")

        self.done_run = make_run(project=self.project, name="Nightly regression")
        self.done_run.status = TestRunStatus.DONE
        self.done_run.started_at = timezone.now()
        self.done_run.finished_at = timezone.now()
        self.done_run.save()
        passed_pivot = add_case_to_run(
            test_run=self.done_run, test_case=self.passed_case
        )
        finish_pivot(passed_pivot, status=TestRunTestCaseStatus.SUCCESS)
        self.failed_pivot = add_case_to_run(
            test_run=self.done_run, test_case=self.failed_case
        )
        self.failed_pivot.result = "Timed out waiting for the export dialog."
        self.failed_pivot.save(update_fields=["result"])
        finish_pivot(self.failed_pivot, status=TestRunTestCaseStatus.FAILED)

        self.draft_run = make_run(project=self.project, name="Payments deep pass")
        self.draft_pivot = add_case_to_run(
            test_run=self.draft_run, test_case=self.never_run_case
        )

        TestCaseUpload.objects.create(
            project=self.project,
            uploaded_by=self.user,
            original_filename="regression-suite.xml",
            status=UploadStatus.COMPLETED,
            total_cases=3,
            processed_cases=3,
        )

        self.client = Client()
        self.client.force_login(self.user)

    def _page_urls(self) -> dict[str, str]:
        project_id = self.project.id
        return {
            "overview": reverse("projects:detail", args=[project_id]),
            "environment": reverse("projects:environment", args=[project_id]),
            "application_context": reverse(
                "projects:application_context", args=[project_id]
            ),
            "test_cases": reverse("projects:test_case_list", args=[project_id]),
            "import": reverse("projects:test_case_import", args=[project_id]),
            "runs": reverse("projects:test_run_list", args=[project_id]),
            "done_run": reverse(
                "projects:test_run_detail", args=[project_id, self.done_run.id]
            ),
            "draft_run": reverse(
                "projects:test_run_detail", args=[project_id, self.draft_run.id]
            ),
            "failed_case": reverse(
                "projects:test_run_case_detail",
                args=[project_id, self.done_run.id, self.failed_pivot.id],
            ),
            "draft_case": reverse(
                "projects:test_run_case_detail",
                args=[project_id, self.draft_run.id, self.draft_pivot.id],
            ),
            "setup_run": reverse("projects:setup_run", args=[project_id]),
            "setup_machine": reverse("projects:setup_machine", args=[project_id]),
        }

    def test_member_can_render_every_project_page(self) -> None:
        for name, url in self._page_urls().items():
            with self.subTest(page=name):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Punk Hazard")
                self.assertNotContains(response, "{#")
                self.assertEqual(
                    _count_post_forms(response.content),
                    _count_csrf_tokens(response.content),
                    "every POST form needs a CSRF token",
                )

    def test_pages_show_domain_content(self) -> None:
        urls = self._page_urls()
        self.assertContains(self.client.get(urls["overview"]), "Nightly regression")
        self.assertContains(self.client.get(urls["test_cases"]), "Export PDF")
        self.assertContains(self.client.get(urls["import"]), "regression-suite.xml")
        self.assertContains(self.client.get(urls["runs"]), "Payments deep pass")
        self.assertContains(
            self.client.get(urls["done_run"]), "Timed out waiting for the export"
        )
        self.assertContains(self.client.get(urls["draft_run"]), "Ready to run?")
        self.assertContains(self.client.get(urls["failed_case"]), "Case 2 of 2")

    def test_non_member_gets_404_on_every_project_page(self) -> None:
        client = Client()
        client.force_login(self.other_user)
        for name, url in self._page_urls().items():
            with self.subTest(page=name):
                self.assertEqual(client.get(url).status_code, 404)

    def test_anonymous_user_is_redirected_to_login(self) -> None:
        client = Client()
        for name, url in self._page_urls().items():
            with self.subTest(page=name):
                response = client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response["Location"])


class ProjectListAndDashboardRenderingTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user, name="Acme Billing")
        make_case(project=self.project, title="Sign in")
        run = make_run(project=self.project, name="Smoke pass")
        run.status = TestRunStatus.DONE
        run.started_at = timezone.now()
        run.finished_at = timezone.now()
        run.save()
        archived = make_project(user=self.user, name="Legacy Intranet")
        archived.archived = True
        archived.save()
        self.client = Client()
        self.client.force_login(self.user)

    def test_projects_list_renders_latest_run_and_hides_archived(self) -> None:
        response = self.client.get(reverse("projects:list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Smoke pass")
        self.assertNotContains(response, "Legacy Intranet")
        self.assertNotContains(response, "{#")

    def test_projects_list_shows_archived_when_requested(self) -> None:
        response = self.client.get(reverse("projects:list"), {"archived": "1"})
        self.assertContains(response, "Legacy Intranet")
        self.assertContains(response, "Restore")

    def test_dashboard_renders_recent_project_and_activity(self) -> None:
        response = self.client.get(reverse("dashboard:index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Acme Billing")
        self.assertContains(response, "Smoke pass")
        self.assertNotContains(response, "{#")

    def test_login_page_renders_brand(self) -> None:
        response = Client().get(reverse("accounts:login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "AI testing on real applications.")
