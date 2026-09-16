from __future__ import annotations

from unittest.mock import patch

from cryptography.fernet import Fernet
from django.core.cache import cache
from django.test import TestCase, override_settings

from projects.models import TestCase as TestCaseModel
from projects.models import TestCaseUpload, UploadSource
from projects.secrets import decrypt_secret
from projects.testrail_client import (
    TestRailCase,
    TestRailCaseType,
    TestRailClient,
    TestRailError,
    TestRailPriority,
    TestRailProject,
    TestRailSuite,
)
from projects.testrail_mapping import TestRailFieldMapper
from projects.testrail_services import (
    TestRailImportTarget,
    TestRailNotConfiguredError,
    build_testrail_client,
    build_testrail_import_label,
    check_testrail_connection,
    clear_testrail_settings,
    get_testrail_api_key_hint,
    get_testrail_picker_state,
    resolve_testrail_import,
    resolve_testrail_import_suite,
    save_testrail_settings,
    start_testrail_import,
    testrail_projects_cache_key,
    testrail_projects_error_cache_key,
    upsert_test_cases_from_testrail,
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

    def test_undecryptable_key_behaves_as_unset(self) -> None:
        save_testrail_settings(
            project=self.project,
            url="https://a.testrail.com",
            email="a@b.com",
            api_key="k",
        )
        self.project.testrail_api_key_encrypted = "garbage"
        self.project.save(update_fields=["testrail_api_key_encrypted"])
        self.assertEqual(get_testrail_api_key_hint(self.project), "")
        with self.assertRaises(TestRailNotConfiguredError):
            build_testrail_client(self.project)


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


MAPPER = TestRailFieldMapper.from_vocabularies(
    [TestRailCaseType(6, "Functional")], [TestRailPriority(4, "5 - Must Test")]
)


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY, CACHES=LOCMEM_CACHE)
class PickerStateTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)
        cache.clear()

    def test_unconfigured(self) -> None:
        state = get_testrail_picker_state(self.project)
        self.assertFalse(state.configured)
        self.assertEqual(state.projects, [])

    def test_lists_active_first_then_completed_and_caches(self) -> None:
        save_testrail_settings(
            project=self.project,
            url="https://a.testrail.com",
            email="a@b.com",
            api_key="k",
        )
        projects = [
            TestRailProject(2, "Zeta", 1, True),
            TestRailProject(1, "beta", 3, False),
            TestRailProject(3, "Alpha", 1, False),
        ]
        with patch.object(
            TestRailClient, "get_projects", return_value=projects
        ) as mocked:
            first = get_testrail_picker_state(self.project)
            second = get_testrail_picker_state(self.project)
        self.assertEqual([p.name for p in first.projects], ["Alpha", "beta", "Zeta"])
        self.assertEqual(second.projects, first.projects)
        self.assertEqual(mocked.call_count, 1)

    def test_error_is_reported_not_raised(self) -> None:
        save_testrail_settings(
            project=self.project,
            url="https://a.testrail.com",
            email="a@b.com",
            api_key="k",
        )
        with patch.object(
            TestRailClient, "get_projects", side_effect=TestRailError("nope", 401)
        ):
            state = get_testrail_picker_state(self.project)
        self.assertTrue(state.configured)
        self.assertEqual(state.error, "nope")

    def test_error_is_negatively_cached_and_not_refetched(self) -> None:
        save_testrail_settings(
            project=self.project,
            url="https://a.testrail.com",
            email="a@b.com",
            api_key="k",
        )
        with patch.object(
            TestRailClient, "get_projects", side_effect=TestRailError("nope", 401)
        ) as mocked:
            first = get_testrail_picker_state(self.project)
            second = get_testrail_picker_state(self.project)
        self.assertEqual(mocked.call_count, 1)
        self.assertTrue(second.configured)
        self.assertEqual(second.error, "nope")
        self.assertEqual(first.error, second.error)

    def test_save_clears_the_negative_error_cache(self) -> None:
        cache.set(testrail_projects_error_cache_key(self.project.id), "nope", 60)
        save_testrail_settings(
            project=self.project,
            url="https://a.testrail.com",
            email="a@b.com",
            api_key="k",
        )
        self.assertIsNone(cache.get(testrail_projects_error_cache_key(self.project.id)))

    def test_stale_cache_shape_is_dropped_and_refetched(self) -> None:
        save_testrail_settings(
            project=self.project,
            url="https://a.testrail.com",
            email="a@b.com",
            api_key="k",
        )
        cache.set(
            testrail_projects_cache_key(self.project.id),
            [{"id": 1, "bogus": True}],
            300,
        )
        fresh = [TestRailProject(1, "Fresh", 1, False)]
        with patch.object(TestRailClient, "get_projects", return_value=fresh) as mocked:
            state = get_testrail_picker_state(self.project)
        self.assertEqual(state.projects, fresh)
        self.assertEqual(mocked.call_count, 1)


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY, CACHES=LOCMEM_CACHE)
class ResolveImportTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)
        save_testrail_settings(
            project=self.project,
            url="https://a.testrail.com",
            email="a@b.com",
            api_key="k",
        )
        cache.clear()
        self.single = TestRailProject(164, "Ext JS 8.1", 1, False)
        self.multi = TestRailProject(15, "ExtJS 6", 3, False)

    def test_single_suite_project_resolves_to_one_suite(self) -> None:
        master = TestRailSuite(6546, "Master", True)
        with patch.object(
            TestRailClient, "get_projects", return_value=[self.single]
        ), patch.object(TestRailClient, "get_suites", return_value=[master]):
            resolution = resolve_testrail_import(self.project, 164)
        self.assertEqual(resolution.single_suite, master)

    def test_multi_suite_project_exposes_suites(self) -> None:
        suites = [
            TestRailSuite(20, "Features", False),
            TestRailSuite(22, "Regression", False),
        ]
        with patch.object(
            TestRailClient, "get_projects", return_value=[self.multi]
        ), patch.object(TestRailClient, "get_suites", return_value=suites):
            resolution = resolve_testrail_import(self.project, 15)
        self.assertIsNone(resolution.single_suite)
        self.assertEqual(resolution.suites, suites)

    def test_unknown_project_raises_404(self) -> None:
        with patch.object(TestRailClient, "get_projects", return_value=[self.single]):
            with self.assertRaises(TestRailError) as ctx:
                resolve_testrail_import(self.project, 999)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_resolve_suite_validates_membership(self) -> None:
        suites = [TestRailSuite(20, "Features", False)]
        with patch.object(
            TestRailClient, "get_projects", return_value=[self.multi]
        ), patch.object(TestRailClient, "get_suites", return_value=suites):
            target = resolve_testrail_import_suite(self.project, 15, 20)
            self.assertEqual(target.suite.id, 20)
            with self.assertRaises(TestRailError) as ctx:
                resolve_testrail_import_suite(self.project, 15, 21)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_label(self) -> None:
        self.assertEqual(
            build_testrail_import_label(
                self.multi, TestRailSuite(20, "Features", False)
            ),
            "TestRail: ExtJS 6 / Features",
        )

    def test_label_is_bounded_to_255_chars_and_keeps_suite_tail(self) -> None:
        long_project = TestRailProject(1, "P" * 300, 1, False)
        long_suite = TestRailSuite(2, "S" * 300, False)
        label = build_testrail_import_label(long_project, long_suite)
        self.assertLessEqual(len(label), 255)
        self.assertTrue(label.endswith("S"))


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY, CACHES=LOCMEM_CACHE)
class StartImportTests(TestCase):
    def test_creates_row_and_dispatches_task(self) -> None:
        user = make_user()
        project = make_project(user=user)
        target = TestRailImportTarget(
            testrail_project=TestRailProject(15, "ExtJS 6", 3, False),
            suite=TestRailSuite(20, "Features", False),
        )
        with patch("projects.testrail_services.celery_app.send_task") as send_task:
            send_task.return_value.id = "task-123"
            upload = start_testrail_import(project=project, user=user, target=target)
        send_task.assert_called_once_with(
            "projects.tasks.import_testrail_cases", args=[upload.id]
        )
        upload.refresh_from_db()
        self.assertEqual(upload.source, UploadSource.TESTRAIL_API)
        self.assertEqual(upload.original_filename, "TestRail: ExtJS 6 / Features")
        self.assertEqual(upload.testrail_project_id, 15)
        self.assertEqual(upload.testrail_suite_id, 20)
        self.assertEqual(upload.status, "processing")
        self.assertEqual(upload.celery_task_id, "task-123")
        self.assertFalse(upload.file)


class UpsertTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)
        self.upload = TestCaseUpload.objects.create(
            project=self.project,
            uploaded_by=self.user,
            original_filename="TestRail: P / S",
            source=UploadSource.TESTRAIL_API,
        )

    def test_creates_missing_cases_linked_to_upload(self) -> None:
        counts = upsert_test_cases_from_testrail(
            upload=self.upload,
            project=self.project,
            cases=[_tr_case(1, "A"), _tr_case(2, "B")],
            mapper=MAPPER,
        )
        self.assertEqual((counts.created, counts.updated), (2, 0))
        self.assertEqual(self.upload.test_cases.count(), 2)
        self.assertEqual(
            set(
                TestCaseModel.objects.filter(project=self.project).values_list(
                    "testrail_id", flat=True
                )
            ),
            {"1", "2"},
        )

    def test_updates_existing_in_place_and_keeps_original_upload(self) -> None:
        older = TestCaseUpload.objects.create(
            project=self.project, uploaded_by=self.user, original_filename="old.xml"
        )
        existing = TestCaseModel.objects.create(
            project=self.project, upload=older, testrail_id="1", title="Old title"
        )
        counts = upsert_test_cases_from_testrail(
            upload=self.upload,
            project=self.project,
            cases=[_tr_case(1, "New title"), _tr_case(2, "B")],
            mapper=MAPPER,
        )
        self.assertEqual((counts.created, counts.updated), (1, 1))
        existing.refresh_from_db()
        self.assertEqual(existing.title, "New title")
        self.assertEqual(existing.upload_id, older.id)
        self.assertEqual(self.upload.test_cases.count(), 1)

    def test_does_not_touch_other_projects(self) -> None:
        other_project = make_project(user=self.user, name="Other")
        foreign = TestCaseModel.objects.create(
            project=other_project, testrail_id="1", title="Foreign"
        )
        upsert_test_cases_from_testrail(
            upload=self.upload,
            project=self.project,
            cases=[_tr_case(1, "A")],
            mapper=MAPPER,
        )
        foreign.refresh_from_db()
        self.assertEqual(foreign.title, "Foreign")

    def test_updates_all_duplicates(self) -> None:
        TestCaseModel.objects.create(
            project=self.project, testrail_id="1", title="dup1"
        )
        TestCaseModel.objects.create(
            project=self.project, testrail_id="1", title="dup2"
        )
        counts = upsert_test_cases_from_testrail(
            upload=self.upload,
            project=self.project,
            cases=[_tr_case(1, "Fresh")],
            mapper=MAPPER,
        )
        self.assertEqual((counts.created, counts.updated), (0, 1))
        titles = set(
            TestCaseModel.objects.filter(project=self.project).values_list(
                "title", flat=True
            )
        )
        self.assertEqual(titles, {"Fresh"})

    def test_progress_callback_receives_cumulative_counts(self) -> None:
        TestCaseModel.objects.create(project=self.project, testrail_id="3", title="x")
        seen: list[tuple[int, int]] = []
        upsert_test_cases_from_testrail(
            upload=self.upload,
            project=self.project,
            cases=[_tr_case(n, str(n)) for n in range(1, 6)],
            mapper=MAPPER,
            batch_size=2,
            progress_callback=lambda processed, updated: seen.append(
                (processed, updated)
            ),
        )
        self.assertEqual(seen, [(2, 0), (4, 1), (5, 1)])
