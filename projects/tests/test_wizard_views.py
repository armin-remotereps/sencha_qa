from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import Client, TestCase
from django.urls import reverse

from projects.models import Project
from projects.tests.helpers import make_case, make_project, make_user


class ProjectCreateWizardTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()

    def test_anonymous_user_is_redirected_to_login(self) -> None:
        client = Client()
        response = client.get(reverse("projects:create"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)  # type: ignore[attr-defined]  # .url only exists on HttpResponseRedirectBase, which a 302 always is

    def test_get_renders_step_one_in_create_mode(self) -> None:
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("projects:create"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["wizard_step"], 1)
        self.assertEqual(response.context["wizard_mode"], "create")
        self.assertIsNone(response.context["project"])

    def test_invalid_post_rerenders_with_200(self) -> None:
        client = Client()
        client.force_login(self.user)
        response = client.post(reverse("projects:create"), {"name": ""})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["form"].is_valid())

    def test_valid_post_creates_project_and_redirects_to_setup_context(self) -> None:
        client = Client()
        client.force_login(self.user)
        response = client.post(
            reverse("projects:create"), {"name": "New Project", "tags": ""}
        )
        project = Project.objects.get(name="New Project")
        self.assertRedirects(
            response, reverse("projects:setup_context", args=[project.id])
        )
        self.assertIn(self.user, project.members.all())


class SetupDetailsWizardTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)
        self.other_user = make_user(email="other@example.com")

    def test_non_member_gets_404(self) -> None:
        client = Client()
        client.force_login(self.other_user)
        response = client.get(reverse("projects:setup_details", args=[self.project.id]))
        self.assertEqual(response.status_code, 404)

    def test_get_renders_edit_mode_with_prefilled_name(self) -> None:
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("projects:setup_details", args=[self.project.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["wizard_mode"], "edit")
        self.assertEqual(response.context["form"].initial["name"], self.project.name)

    def test_valid_post_updates_project_and_redirects(self) -> None:
        client = Client()
        client.force_login(self.user)
        response = client.post(
            reverse("projects:setup_details", args=[self.project.id]),
            {"name": "Renamed", "tags": ""},
        )
        self.assertRedirects(
            response, reverse("projects:setup_context", args=[self.project.id])
        )
        self.project.refresh_from_db()
        self.assertEqual(self.project.name, "Renamed")


class SetupContextWizardTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)

    def test_valid_post_saves_context_and_redirects_to_setup_cases(self) -> None:
        client = Client()
        client.force_login(self.user)
        response = client.post(
            reverse("projects:setup_context", args=[self.project.id]),
            {
                "application_url": "https://app.example.com",
                "application_platform": "web",
                "project_prompt": "Some notes",
            },
        )
        self.assertRedirects(
            response, reverse("projects:setup_cases", args=[self.project.id])
        )
        self.project.refresh_from_db()
        self.assertEqual(self.project.application_url, "https://app.example.com")

    def test_invalid_post_rerenders_200(self) -> None:
        client = Client()
        client.force_login(self.user)
        response = client.post(
            reverse("projects:setup_context", args=[self.project.id]),
            {"application_url": "not-a-url", "application_platform": "web"},
        )
        self.assertEqual(response.status_code, 200)


class SetupCasesWizardTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)

    def test_get_renders_case_count(self) -> None:
        make_case(project=self.project)
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("projects:setup_cases", args=[self.project.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["case_count"], 1)

    def test_post_without_file_shows_error_and_redirects_back(self) -> None:
        client = Client()
        client.force_login(self.user)
        response = client.post(reverse("projects:setup_cases", args=[self.project.id]))
        self.assertRedirects(
            response, reverse("projects:setup_cases", args=[self.project.id])
        )


class SetupMachineWizardTests(TestCase):
    def test_get_renders_step_four(self) -> None:
        user = make_user()
        project = make_project(user=user)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("projects:setup_machine", args=[project.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["wizard_step"], 4)


class SetupRunWizardTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)
        self.case = make_case(project=self.project)

    def test_get_includes_suggested_name_and_blocking_readiness(self) -> None:
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("projects:setup_run", args=[self.project.id]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["readiness"].ready)

    def test_post_with_blocking_readiness_rerenders_200(self) -> None:
        client = Client()
        client.force_login(self.user)
        response = client.post(
            reverse("projects:setup_run", args=[self.project.id]),
            {"name": "First run", "test_case_ids": [str(self.case.id)]},
        )
        # The test machine is disconnected, so readiness still blocks starting.
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["readiness"].blockers)
        # The picker keeps the submitted selection and explains why nothing started.
        self.assertEqual(response.context["selected_ids"], frozenset({self.case.id}))
        self.assertContains(response, "resolve the failed checks below")
        self.assertContains(response, f'value="{self.case.id}" checked')

    @patch("projects.views.start_test_run")
    def test_happy_path_creates_run_and_redirects(
        self, mock_start_test_run: MagicMock
    ) -> None:
        client = Client()
        client.force_login(self.user)
        self.project.agent_connected = True
        self.project.agent_omniparser_status = {
            "state": "ready",
            "message": "",
            "device": "cpu",
            "weights_dir": "",
            "phase": "ready",
            "load_seconds": 1.0,
        }
        self.project.save()

        response = client.post(
            reverse("projects:setup_run", args=[self.project.id]),
            {"name": "First run", "test_case_ids": [str(self.case.id)]},
        )

        mock_start_test_run.assert_called_once()
        test_run = self.project.test_runs.get(name="First run")
        self.assertRedirects(
            response,
            reverse(
                "projects:test_run_detail",
                args=[self.project.id, test_run.id],
            ),
        )
