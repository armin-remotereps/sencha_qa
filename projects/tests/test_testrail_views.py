from __future__ import annotations

from unittest.mock import patch

from cryptography.fernet import Fernet
from django.contrib.messages import get_messages
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from projects.models import TestCaseUpload, UploadSource
from projects.testrail_client import TestRailError, TestRailProject, TestRailSuite
from projects.testrail_services import (
    TestRailConnectionResult,
    TestRailImportResolution,
    TestRailImportTarget,
    TestRailPickerState,
    get_testrail_api_key_hint,
    save_testrail_settings,
)
from projects.tests.helpers import make_project, make_user

TEST_KEY = Fernet.generate_key().decode("ascii")
LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY, CACHES=LOCMEM_CACHE)
class SettingsPageTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)
        self.client = Client()
        self.client.force_login(self.user)
        self.url = reverse("projects:settings", args=[self.project.id])

    def test_anonymous_redirects_to_login(self) -> None:
        response = Client().get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_non_member_gets_404(self) -> None:
        other = Client()
        other.force_login(make_user(email="other@example.com"))
        self.assertEqual(other.get(self.url).status_code, 404)

    def test_get_renders_settings_tab(self) -> None:
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_tab"], "settings")
        self.assertContains(response, "TestRail integration")

    def test_post_saves_all_three_fields(self) -> None:
        response = self.client.post(
            self.url,
            {
                "testrail_url": "https://sencha.testrail.com/",
                "testrail_email": "qa@example.com",
                "testrail_api_key": "abc123",
            },
        )
        self.assertRedirects(response, self.url)
        self.project.refresh_from_db()
        self.assertEqual(self.project.testrail_url, "https://sencha.testrail.com")
        self.assertEqual(get_testrail_api_key_hint(self.project), "123")

    def test_first_save_requires_api_key(self) -> None:
        response = self.client.post(
            self.url,
            {
                "testrail_url": "https://sencha.testrail.com",
                "testrail_email": "qa@example.com",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "API key is required")
        self.project.refresh_from_db()
        self.assertFalse(self.project.has_testrail_settings)

    def test_later_save_without_key_keeps_key(self) -> None:
        save_testrail_settings(
            project=self.project,
            url="https://a.testrail.com",
            email="a@b.com",
            api_key="keep-me",
        )
        response = self.client.post(
            self.url,
            {"testrail_url": "https://b.testrail.com", "testrail_email": "c@d.com"},
        )
        self.assertRedirects(response, self.url)
        self.project.refresh_from_db()
        self.assertEqual(self.project.testrail_url, "https://b.testrail.com")
        self.assertEqual(get_testrail_api_key_hint(self.project), "-me")

    def test_rejects_non_http_url(self) -> None:
        response = self.client.post(
            self.url,
            {
                "testrail_url": "ftp://x",
                "testrail_email": "a@b.com",
                "testrail_api_key": "k",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter a valid URL")

    def test_form_never_echoes_key(self) -> None:
        save_testrail_settings(
            project=self.project,
            url="https://a.testrail.com",
            email="a@b.com",
            api_key="topsecret",
        )
        response = self.client.get(self.url)
        self.assertNotContains(response, "topsecret")
        self.assertContains(response, "ends in …ret")


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY, CACHES=LOCMEM_CACHE)
class TestConnectionViewTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)
        self.client = Client()
        self.client.force_login(self.user)
        self.url = reverse("projects:settings_testrail_test", args=[self.project.id])

    def test_get_not_allowed(self) -> None:
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_success_message(self) -> None:
        with patch(
            "projects.testrail_views.check_testrail_connection",
            return_value=TestRailConnectionResult(
                True, "Connected. 3 TestRail projects visible to this account.", 3
            ),
        ):
            response = self.client.post(self.url)
        self.assertRedirects(
            response, reverse("projects:settings", args=[self.project.id])
        )
        messages = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertIn(
            "Connected. 3 TestRail projects visible to this account.", messages
        )

    def test_failure_message(self) -> None:
        with patch(
            "projects.testrail_views.check_testrail_connection",
            return_value=TestRailConnectionResult(
                False, "Authentication failed: bad key"
            ),
        ):
            response = self.client.post(self.url)
        messages = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertIn("Authentication failed: bad key", messages)


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY, CACHES=LOCMEM_CACHE)
class ClearSettingsViewTests(TestCase):
    def test_clear_blanks_settings(self) -> None:
        user = make_user()
        project = make_project(user=user)
        save_testrail_settings(
            project=project, url="https://a.testrail.com", email="a@b.com", api_key="k"
        )
        client = Client()
        client.force_login(user)
        response = client.post(
            reverse("projects:settings_testrail_clear", args=[project.id])
        )
        self.assertRedirects(response, reverse("projects:settings", args=[project.id]))
        project.refresh_from_db()
        self.assertFalse(project.has_testrail_settings)


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY, CACHES=LOCMEM_CACHE)
class ImportPageCardTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)
        self.client = Client()
        self.client.force_login(self.user)
        self.url = reverse("projects:test_case_import", args=[self.project.id])

    def test_unconfigured_card_links_to_settings(self) -> None:
        response = self.client.get(self.url)
        self.assertContains(
            response, reverse("projects:settings", args=[self.project.id])
        )
        self.assertContains(response, "Import from TestRail account")

    def test_configured_card_lists_projects(self) -> None:
        state = TestRailPickerState(
            configured=True,
            projects=[
                TestRailProject(15, "ExtJS 6", 3, False),
                TestRailProject(2, "Old", 1, True),
            ],
        )
        with patch("projects.views.get_testrail_picker_state", return_value=state):
            response = self.client.get(self.url)
        self.assertContains(response, '<option value="15">ExtJS 6</option>', html=True)
        self.assertContains(
            response, '<option value="2">Old (completed)</option>', html=True
        )

    def test_error_shown_inline(self) -> None:
        state = TestRailPickerState(
            configured=True, projects=[], error="Authentication failed: bad key"
        )
        with patch("projects.views.get_testrail_picker_state", return_value=state):
            response = self.client.get(self.url)
        self.assertContains(response, "Authentication failed: bad key")

    def test_history_row_shows_new_and_updated_for_api_imports(self) -> None:
        TestCaseUpload.objects.create(
            project=self.project,
            uploaded_by=self.user,
            original_filename="TestRail: P / S",
            source=UploadSource.TESTRAIL_API,
            status="completed",
            total_cases=10,
            processed_cases=10,
            updated_cases=3,
        )
        response = self.client.get(self.url)
        self.assertContains(response, "7 new")
        self.assertContains(response, "3 updated")


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY, CACHES=LOCMEM_CACHE)
class ImportStartViewTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)
        self.client = Client()
        self.client.force_login(self.user)
        self.start_url = reverse(
            "projects:testrail_import_start", args=[self.project.id]
        )
        self.import_url = reverse("projects:test_case_import", args=[self.project.id])
        self.multi = TestRailProject(15, "ExtJS 6", 3, False)
        self.single = TestRailProject(164, "Ext JS 8.1", 1, False)

    def test_get_not_allowed(self) -> None:
        self.assertEqual(self.client.get(self.start_url).status_code, 405)

    def test_invalid_form_redirects_with_error(self) -> None:
        response = self.client.post(self.start_url, {"testrail_project_id": "abc"})
        self.assertRedirects(response, self.import_url)
        messages = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any("TestRail project" in m for m in messages))

    def test_single_suite_starts_import_immediately(self) -> None:
        master = TestRailSuite(6546, "Master", True)
        resolution = TestRailImportResolution(
            testrail_project=self.single, suites=[master]
        )
        with patch(
            "projects.testrail_views.resolve_testrail_import", return_value=resolution
        ), patch("projects.testrail_views.start_testrail_import") as start:
            response = self.client.post(self.start_url, {"testrail_project_id": "164"})
        self.assertRedirects(response, self.import_url)
        start.assert_called_once()
        self.assertEqual(
            start.call_args.kwargs["target"], TestRailImportTarget(self.single, master)
        )

    def test_multi_suite_renders_picker(self) -> None:
        suites = [
            TestRailSuite(20, "Features", False),
            TestRailSuite(22, "Regression", False),
        ]
        resolution = TestRailImportResolution(
            testrail_project=self.multi, suites=suites
        )
        with patch(
            "projects.testrail_views.resolve_testrail_import", return_value=resolution
        ):
            response = self.client.post(self.start_url, {"testrail_project_id": "15"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, '<option value="22">Regression</option>', html=True
        )
        self.assertContains(
            response,
            reverse("projects:testrail_import_suite", args=[self.project.id, 15]),
        )

    def test_testrail_error_redirects_with_message(self) -> None:
        with patch(
            "projects.testrail_views.resolve_testrail_import",
            side_effect=TestRailError("Authentication failed: bad key", 401),
        ):
            response = self.client.post(self.start_url, {"testrail_project_id": "15"})
        self.assertRedirects(response, self.import_url)
        messages = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertIn("Authentication failed: bad key", messages)

    def test_suite_post_starts_import(self) -> None:
        target = TestRailImportTarget(self.multi, TestRailSuite(20, "Features", False))
        suite_url = reverse(
            "projects:testrail_import_suite", args=[self.project.id, 15]
        )
        with patch(
            "projects.testrail_views.resolve_testrail_import_suite", return_value=target
        ) as resolve, patch("projects.testrail_views.start_testrail_import") as start:
            response = self.client.post(suite_url, {"testrail_suite_id": "20"})
        self.assertRedirects(response, self.import_url)
        resolve.assert_called_once_with(self.project, 15, 20)
        self.assertEqual(start.call_args.kwargs["target"], target)

    def test_suite_post_invalid_redirects(self) -> None:
        suite_url = reverse(
            "projects:testrail_import_suite", args=[self.project.id, 15]
        )
        response = self.client.post(suite_url, {"testrail_suite_id": ""})
        self.assertRedirects(response, self.import_url)
