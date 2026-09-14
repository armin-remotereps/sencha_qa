from __future__ import annotations

from unittest.mock import patch

from cryptography.fernet import Fernet
from django.core.cache import cache
from django.test import TestCase, override_settings

from projects.models import TestCaseUpload, UploadSource
from projects.secrets import decrypt_secret
from projects.testrail_client import TestRailClient, TestRailError, TestRailProject
from projects.testrail_services import (
    TestRailNotConfiguredError,
    build_testrail_client,
    check_testrail_connection,
    clear_testrail_settings,
    get_testrail_api_key_hint,
    save_testrail_settings,
    testrail_projects_cache_key,
)
from projects.tests.helpers import make_project, make_user

TEST_KEY = Fernet.generate_key().decode("ascii")
LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class ProjectTestRailSettingsModelTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)

    def test_has_testrail_settings_false_by_default(self) -> None:
        self.assertFalse(self.project.has_testrail_settings)

    def test_has_testrail_settings_requires_all_three(self) -> None:
        self.project.testrail_url = "https://x.testrail.com"
        self.project.testrail_email = "a@b.com"
        self.assertFalse(self.project.has_testrail_settings)
        self.project.testrail_api_key_encrypted = "token"
        self.assertTrue(self.project.has_testrail_settings)


class TestCaseUploadSourceTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)

    def test_default_source_is_xml(self) -> None:
        upload = TestCaseUpload.objects.create(
            project=self.project, uploaded_by=self.user, original_filename="a.xml"
        )
        self.assertEqual(upload.source, UploadSource.XML)
        self.assertFalse(upload.is_testrail_api)

    def test_api_upload_without_file_and_counts(self) -> None:
        upload = TestCaseUpload.objects.create(
            project=self.project,
            uploaded_by=self.user,
            original_filename="TestRail: P / S",
            source=UploadSource.TESTRAIL_API,
            testrail_project_id=146,
            testrail_suite_id=1162,
            processed_cases=10,
            updated_cases=3,
        )
        self.assertTrue(upload.is_testrail_api)
        self.assertEqual(upload.created_cases, 7)
        self.assertFalse(upload.file)


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY, CACHES=LOCMEM_CACHE)
class SaveTestRailSettingsTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)
        cache.clear()

    def test_saves_encrypted_key_and_normalizes_url(self) -> None:
        save_testrail_settings(
            project=self.project,
            url="https://sencha.testrail.com/",
            email="qa@example.com",
            api_key="secret-key",
        )
        self.project.refresh_from_db()
        self.assertEqual(self.project.testrail_url, "https://sencha.testrail.com")
        self.assertEqual(self.project.testrail_email, "qa@example.com")
        self.assertNotEqual(self.project.testrail_api_key_encrypted, "secret-key")
        self.assertEqual(
            decrypt_secret(self.project.testrail_api_key_encrypted), "secret-key"
        )
        self.assertEqual(get_testrail_api_key_hint(self.project), "key")

    def test_none_api_key_keeps_existing(self) -> None:
        save_testrail_settings(
            project=self.project,
            url="https://a.testrail.com",
            email="a@b.com",
            api_key="first",
        )
        stored = self.project.testrail_api_key_encrypted
        save_testrail_settings(
            project=self.project,
            url="https://b.testrail.com",
            email="c@d.com",
            api_key=None,
        )
        self.project.refresh_from_db()
        self.assertEqual(self.project.testrail_api_key_encrypted, stored)
        self.assertEqual(self.project.testrail_url, "https://b.testrail.com")

    def test_save_invalidates_picker_cache(self) -> None:
        cache.set(testrail_projects_cache_key(self.project.id), ["stale"], 300)
        save_testrail_settings(
            project=self.project,
            url="https://a.testrail.com",
            email="a@b.com",
            api_key="k",
        )
        self.assertIsNone(cache.get(testrail_projects_cache_key(self.project.id)))

    def test_clear_blanks_everything(self) -> None:
        save_testrail_settings(
            project=self.project,
            url="https://a.testrail.com",
            email="a@b.com",
            api_key="k",
        )
        clear_testrail_settings(self.project)
        self.project.refresh_from_db()
        self.assertFalse(self.project.has_testrail_settings)
        self.assertEqual(get_testrail_api_key_hint(self.project), "")

    def test_build_client_requires_settings(self) -> None:
        with self.assertRaises(TestRailNotConfiguredError):
            build_testrail_client(self.project)

    def test_build_client_returns_client(self) -> None:
        save_testrail_settings(
            project=self.project,
            url="https://a.testrail.com",
            email="a@b.com",
            api_key="k",
        )
        self.assertIsInstance(build_testrail_client(self.project), TestRailClient)


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY, CACHES=LOCMEM_CACHE)
class CheckConnectionTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)
        save_testrail_settings(
            project=self.project,
            url="https://a.testrail.com",
            email="a@b.com",
            api_key="k",
        )

    def test_not_configured_reports_failure(self) -> None:
        clear_testrail_settings(self.project)
        result = check_testrail_connection(self.project)
        self.assertFalse(result.ok)
        self.assertIn("Settings", result.message)

    def test_success_reports_project_count(self) -> None:
        projects = [TestRailProject(1, "A", 1, False), TestRailProject(2, "B", 3, True)]
        with patch.object(TestRailClient, "get_projects", return_value=projects):
            result = check_testrail_connection(self.project)
        self.assertTrue(result.ok)
        self.assertEqual(result.project_count, 2)
        self.assertIn("2 TestRail projects", result.message)

    def test_testrail_error_reports_message(self) -> None:
        with patch.object(
            TestRailClient,
            "get_projects",
            side_effect=TestRailError("Authentication failed: bad key", 401),
        ):
            result = check_testrail_connection(self.project)
        self.assertFalse(result.ok)
        self.assertEqual(result.message, "Authentication failed: bad key")
