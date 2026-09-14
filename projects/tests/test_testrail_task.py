from __future__ import annotations

from unittest.mock import patch

from cryptography.fernet import Fernet
from django.test import TestCase, override_settings

from projects.models import TestCase as TestCaseModel
from projects.models import TestCaseUpload, UploadSource, UploadStatus
from projects.tasks import import_testrail_cases
from projects.testrail_client import (
    TestRailCase,
    TestRailCaseType,
    TestRailClient,
    TestRailError,
    TestRailPriority,
)
from projects.testrail_services import save_testrail_settings
from projects.tests.helpers import make_project, make_user

TEST_KEY = Fernet.generate_key().decode("ascii")
LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def _tr_case(case_id: int, title: str) -> TestRailCase:
    return TestRailCase(
        id=case_id,
        title=title,
        template_id=1,
        type_id=6,
        priority_id=4,
        refs="",
        estimate="",
        preconditions="",
        steps="s",
        expected="e",
        steps_separated=(),
    )


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY, CACHES=LOCMEM_CACHE)
class ImportTestRailCasesTaskTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)
        save_testrail_settings(
            project=self.project,
            url="https://a.testrail.com",
            email="a@b.com",
            api_key="k",
        )
        self.upload = TestCaseUpload.objects.create(
            project=self.project,
            uploaded_by=self.user,
            original_filename="TestRail: P / S",
            source=UploadSource.TESTRAIL_API,
            testrail_project_id=15,
            testrail_suite_id=20,
            status=UploadStatus.PROCESSING,
        )
        self.progress = patch("projects.tasks._send_upload_progress").start()
        self.addCleanup(patch.stopall)

    def test_success_creates_and_updates_and_marks_completed(self) -> None:
        TestCaseModel.objects.create(project=self.project, testrail_id="1", title="old")
        with patch.object(
            TestRailClient,
            "get_case_types",
            return_value=[TestRailCaseType(6, "Functional")],
        ), patch.object(
            TestRailClient,
            "get_priorities",
            return_value=[TestRailPriority(4, "5 - Must Test")],
        ), patch.object(
            TestRailClient,
            "iter_cases",
            return_value=iter([_tr_case(1, "new"), _tr_case(2, "B")]),
        ):
            import_testrail_cases.apply(args=(self.upload.id,))
        self.upload.refresh_from_db()
        self.assertEqual(self.upload.status, UploadStatus.COMPLETED)
        self.assertEqual(self.upload.total_cases, 2)
        self.assertEqual(self.upload.processed_cases, 2)
        self.assertEqual(self.upload.updated_cases, 1)
        self.assertEqual(self.upload.created_cases, 1)
        self.assertEqual(TestCaseModel.objects.get(testrail_id="1").title, "new")
        self.assertTrue(self.progress.called)

    def test_testrail_error_marks_failed_with_message_and_deletes_created_only(
        self,
    ) -> None:
        pre_existing = TestCaseModel.objects.create(
            project=self.project, testrail_id="9", title="keep"
        )
        self.upload.test_cases.create(
            project=self.project, testrail_id="1", title="partial"
        )

        def explode(*_args: object, **_kwargs: object) -> None:
            raise TestRailError("Authentication failed: bad key", 401)

        with patch.object(TestRailClient, "get_case_types", side_effect=explode):
            import_testrail_cases.apply(args=(self.upload.id,))
        self.upload.refresh_from_db()
        self.assertEqual(self.upload.status, UploadStatus.FAILED)
        self.assertEqual(self.upload.error_message, "Authentication failed: bad key")
        self.assertEqual(self.upload.test_cases.count(), 0)
        self.assertTrue(TestCaseModel.objects.filter(pk=pre_existing.pk).exists())

    def test_unexpected_error_uses_generic_message(self) -> None:
        with patch.object(
            TestRailClient, "get_case_types", side_effect=RuntimeError("boom")
        ):
            import_testrail_cases.apply(args=(self.upload.id,))
        self.upload.refresh_from_db()
        self.assertEqual(self.upload.status, UploadStatus.FAILED)
        self.assertEqual(
            self.upload.error_message, "An error occurred while processing the upload."
        )

    def test_missing_upload_is_a_noop(self) -> None:
        import_testrail_cases.apply(args=(999_999,))
        self.progress.assert_not_called()
