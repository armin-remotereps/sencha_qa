from __future__ import annotations

from django.test import TestCase

from projects.models import TestCaseUpload, UploadSource
from projects.tests.helpers import make_project, make_user


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
