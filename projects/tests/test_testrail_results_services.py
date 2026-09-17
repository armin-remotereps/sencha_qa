from __future__ import annotations

from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet
from django.test import TestCase, override_settings

from projects.models import TestCase as TestCaseModel
from projects.models import (
    TestCaseUpload,
    TestRailPushStatus,
    TestRun,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
    UploadSource,
)
from projects.testrail_client import TestRailError, TestRailResult, TestRailRun
from projects.testrail_mapping import build_result_comment, comment_hash
from projects.testrail_results_services import (
    TestRailPushOutcome,
    can_push_testrail_results,
    get_testrail_push_panel,
    mark_testrail_push_failed,
    mark_testrail_push_succeeded,
    plan_testrail_push,
    push_test_run_results,
    start_testrail_results_push,
    suggest_testrail_target,
)
from projects.testrail_services import (
    TestRailNotConfiguredError,
    TestRailPickerState,
    save_testrail_settings,
)
from projects.tests.helpers import (
    add_case_to_run,
    finish_pivot,
    make_case,
    make_project,
    make_run,
    make_user,
)

TEST_KEY = Fernet.generate_key().decode("ascii")
LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
LINKS_BASE_URL = "https://app.example.com/"
BUILD_CLIENT_PATH = "projects.testrail_results_services.build_testrail_client"
CHANNEL_LAYER_PATH = "projects.testrail_results_services.get_channel_layer"


def _client_context(client: MagicMock) -> MagicMock:
    """Wrap a mock TestRailClient as the context manager build_testrail_client returns."""
    context = MagicMock()
    context.__enter__.return_value = client
    context.__exit__.return_value = False
    return context


def _set_result(pivot: TestRunTestCase, *, text: str) -> None:
    pivot.result = text
    pivot.save(update_fields=["result", "updated_at"])


def _mark_previously_pushed(
    pivot: TestRunTestCase, *, status_id: int, comment: str
) -> None:
    pivot.testrail_result_id = 1
    pivot.testrail_pushed_status_id = status_id
    pivot.testrail_pushed_comment_hash = comment_hash(comment)
    pivot.save(
        update_fields=[
            "testrail_result_id",
            "testrail_pushed_status_id",
            "testrail_pushed_comment_hash",
            "updated_at",
        ]
    )


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY, CACHES=LOCMEM_CACHE)
@patch(CHANNEL_LAYER_PATH, return_value=None)
class PlanTestrailPushTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)
        self.test_run = make_run(project=self.project)

    def _pivot(
        self, *, testrail_id: str, status: TestRunTestCaseStatus, result: str
    ) -> TestRunTestCase:
        case = make_case(project=self.project)
        case.testrail_id = testrail_id
        case.save(update_fields=["testrail_id", "updated_at"])
        pivot = add_case_to_run(test_run=self.test_run, test_case=case)
        finish_pivot(pivot, status=status)
        _set_result(pivot, text=result)
        return pivot

    def test_new_result_is_changed(self, _mock_layer: MagicMock) -> None:
        self._pivot(
            testrail_id="10", status=TestRunTestCaseStatus.SUCCESS, result="All good"
        )

        plan = plan_testrail_push(self.test_run, links_base_url=LINKS_BASE_URL)

        self.assertEqual(len(plan.results), 1)
        self.assertEqual(len(plan.changed), 1)
        self.assertEqual(plan.skipped_no_case_id, 0)
        self.assertEqual(plan.skipped_not_pushable, 0)

    def test_identical_status_and_comment_is_unchanged(
        self, _mock_layer: MagicMock
    ) -> None:
        pivot = self._pivot(
            testrail_id="10", status=TestRunTestCaseStatus.SUCCESS, result="All good"
        )
        first_plan = plan_testrail_push(self.test_run, links_base_url=LINKS_BASE_URL)
        planned = first_plan.results[0]
        _mark_previously_pushed(
            pivot, status_id=planned.status_id, comment=planned.comment
        )

        second_plan = plan_testrail_push(self.test_run, links_base_url=LINKS_BASE_URL)

        self.assertEqual(len(second_plan.results), 1)
        self.assertEqual(len(second_plan.changed), 0)

    def test_status_change_is_changed(self, _mock_layer: MagicMock) -> None:
        pivot = self._pivot(
            testrail_id="10", status=TestRunTestCaseStatus.SUCCESS, result="All good"
        )
        first_plan = plan_testrail_push(self.test_run, links_base_url=LINKS_BASE_URL)
        planned = first_plan.results[0]
        _mark_previously_pushed(
            pivot, status_id=planned.status_id, comment=planned.comment
        )
        pivot.status = TestRunTestCaseStatus.FAILED
        pivot.save(update_fields=["status", "updated_at"])

        second_plan = plan_testrail_push(self.test_run, links_base_url=LINKS_BASE_URL)

        self.assertEqual(len(second_plan.changed), 1)

    def test_result_text_change_is_changed(self, _mock_layer: MagicMock) -> None:
        pivot = self._pivot(
            testrail_id="10", status=TestRunTestCaseStatus.SUCCESS, result="All good"
        )
        first_plan = plan_testrail_push(self.test_run, links_base_url=LINKS_BASE_URL)
        planned = first_plan.results[0]
        _mark_previously_pushed(
            pivot, status_id=planned.status_id, comment=planned.comment
        )
        _set_result(pivot, text="Different narrative")

        second_plan = plan_testrail_push(self.test_run, links_base_url=LINKS_BASE_URL)

        self.assertEqual(len(second_plan.changed), 1)

    def test_skips_case_without_parseable_testrail_id(
        self, _mock_layer: MagicMock
    ) -> None:
        self._pivot(
            testrail_id="not-a-number",
            status=TestRunTestCaseStatus.SUCCESS,
            result="ok",
        )

        plan = plan_testrail_push(self.test_run, links_base_url=LINKS_BASE_URL)

        self.assertEqual(plan.results, [])
        self.assertEqual(plan.skipped_no_case_id, 1)
        self.assertEqual(plan.skipped_not_pushable, 0)

    def test_skips_status_that_is_not_pushable(self, _mock_layer: MagicMock) -> None:
        self._pivot(testrail_id="10", status=TestRunTestCaseStatus.CANCELLED, result="")

        plan = plan_testrail_push(self.test_run, links_base_url=LINKS_BASE_URL)

        self.assertEqual(plan.results, [])
        self.assertEqual(plan.skipped_not_pushable, 1)
        self.assertEqual(plan.skipped_no_case_id, 0)


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY, CACHES=LOCMEM_CACHE)
class SuggestTestrailTargetTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)
        self.test_run = make_run(project=self.project)

    def _upload(
        self, *, testrail_project_id: int, testrail_suite_id: int
    ) -> TestCaseUpload:
        return TestCaseUpload.objects.create(
            project=self.project,
            uploaded_by=self.user,
            original_filename="TestRail: P / S",
            source=UploadSource.TESTRAIL_API,
            testrail_project_id=testrail_project_id,
            testrail_suite_id=testrail_suite_id,
        )

    def test_picks_the_most_common_pair(self) -> None:
        majority_upload = self._upload(testrail_project_id=7, testrail_suite_id=9)
        minority_upload = self._upload(testrail_project_id=7, testrail_suite_id=10)
        for index in range(2):
            case = TestCaseModel.objects.create(
                project=self.project,
                upload=majority_upload,
                testrail_id=str(100 + index),
            )
            add_case_to_run(test_run=self.test_run, test_case=case)
        minority_case = TestCaseModel.objects.create(
            project=self.project, upload=minority_upload, testrail_id="200"
        )
        add_case_to_run(test_run=self.test_run, test_case=minority_case)

        result = suggest_testrail_target(self.test_run)

        self.assertEqual(result, (7, 9))

    def test_xml_only_cases_return_none_none(self) -> None:
        case = make_case(project=self.project)
        add_case_to_run(test_run=self.test_run, test_case=case)

        result = suggest_testrail_target(self.test_run)

        self.assertEqual(result, (None, None))


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY, CACHES=LOCMEM_CACHE)
@patch(CHANNEL_LAYER_PATH, return_value=None)
class PushTestRunResultsTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)
        self.test_run = make_run(project=self.project)
        self.test_run.status = TestRunStatus.DONE
        self.test_run.testrail_project_id = 7
        self.test_run.testrail_suite_id = 9
        self.test_run.save()

        self.pivot_success = self._pivot(
            testrail_id="10", status=TestRunTestCaseStatus.SUCCESS, result="All good"
        )
        self.pivot_failed = self._pivot(
            testrail_id="11", status=TestRunTestCaseStatus.FAILED, result="Broke"
        )

    def _pivot(
        self, *, testrail_id: str, status: TestRunTestCaseStatus, result: str
    ) -> TestRunTestCase:
        case = make_case(project=self.project)
        case.testrail_id = testrail_id
        case.save(update_fields=["testrail_id", "updated_at"])
        pivot = add_case_to_run(test_run=self.test_run, test_case=case)
        finish_pivot(pivot, status=status)
        _set_result(pivot, text=result)
        return pivot

    def test_first_push_creates_run_and_pushes_every_case(
        self, _mock_layer: MagicMock
    ) -> None:
        client = MagicMock()
        client.add_run.return_value = TestRailRun(
            id=555, name="run", is_completed=False, url=""
        )
        client.add_results_for_cases.return_value = [
            TestRailResult(id=1001, status_id=1),
            TestRailResult(id=1002, status_id=5),
        ]

        with patch(BUILD_CLIENT_PATH, return_value=_client_context(client)):
            outcome = push_test_run_results(
                self.test_run, links_base_url=LINKS_BASE_URL
            )

        client.add_run.assert_called_once()
        call_args, call_kwargs = client.add_run.call_args
        self.assertEqual(call_args[0], 7)
        self.assertEqual(call_kwargs["suite_id"], 9)
        self.assertEqual(
            call_kwargs["name"], f"Punk Hazard: {self.test_run.display_name}"
        )
        self.assertEqual(call_kwargs["case_ids"], [10, 11])
        client.get_run.assert_not_called()

        self.test_run.refresh_from_db()
        self.assertEqual(self.test_run.testrail_run_id, 555)

        self.pivot_success.refresh_from_db()
        self.pivot_failed.refresh_from_db()
        self.assertEqual(self.pivot_success.testrail_result_id, 1001)
        self.assertEqual(self.pivot_success.testrail_pushed_status_id, 1)
        self.assertIsNotNone(self.pivot_success.testrail_pushed_at)
        self.assertEqual(self.pivot_failed.testrail_result_id, 1002)
        self.assertEqual(self.pivot_failed.testrail_pushed_status_id, 5)

        self.assertEqual(
            outcome,
            TestRailPushOutcome(
                pushed=2, unchanged=0, skipped_no_case_id=0, testrail_run_id=555
            ),
        )

    def _push_once(self, client: MagicMock) -> TestRailPushOutcome:
        with patch(BUILD_CLIENT_PATH, return_value=_client_context(client)):
            return push_test_run_results(self.test_run, links_base_url=LINKS_BASE_URL)

    def test_second_push_with_no_changes_updates_run_and_skips_results(
        self, _mock_layer: MagicMock
    ) -> None:
        first_client = MagicMock()
        first_client.add_run.return_value = TestRailRun(
            id=555, name="run", is_completed=False, url=""
        )
        first_client.add_results_for_cases.return_value = [
            TestRailResult(id=1001, status_id=1),
            TestRailResult(id=1002, status_id=5),
        ]
        self._push_once(first_client)
        self.test_run.refresh_from_db()

        second_client = MagicMock()
        second_client.get_run.return_value = TestRailRun(
            id=555, name="run", is_completed=False, url=""
        )

        outcome = self._push_once(second_client)

        second_client.add_run.assert_not_called()
        second_client.get_run.assert_called_once_with(555)
        second_client.update_run.assert_called_once_with(555, case_ids=[10, 11])
        second_client.add_results_for_cases.assert_not_called()
        self.assertEqual(
            outcome,
            TestRailPushOutcome(
                pushed=0, unchanged=2, skipped_no_case_id=0, testrail_run_id=555
            ),
        )

    def test_second_push_sends_only_the_changed_case(
        self, _mock_layer: MagicMock
    ) -> None:
        first_client = MagicMock()
        first_client.add_run.return_value = TestRailRun(
            id=555, name="run", is_completed=False, url=""
        )
        first_client.add_results_for_cases.return_value = [
            TestRailResult(id=1001, status_id=1),
            TestRailResult(id=1002, status_id=5),
        ]
        self._push_once(first_client)
        self.test_run.refresh_from_db()
        _set_result(self.pivot_success, text="Updated narrative")

        second_client = MagicMock()
        second_client.get_run.return_value = TestRailRun(
            id=555, name="run", is_completed=False, url=""
        )
        second_client.add_results_for_cases.return_value = [
            TestRailResult(id=2001, status_id=1)
        ]

        outcome = self._push_once(second_client)

        second_client.add_results_for_cases.assert_called_once()
        run_id_arg, results_arg = second_client.add_results_for_cases.call_args[0]
        self.assertEqual(run_id_arg, 555)
        self.assertEqual(len(results_arg), 1)
        self.assertEqual(results_arg[0].case_id, 10)
        self.assertEqual(outcome.pushed, 1)
        self.assertEqual(outcome.unchanged, 1)

    def test_deleted_run_is_recreated_and_tracking_reset(
        self, _mock_layer: MagicMock
    ) -> None:
        self.test_run.testrail_run_id = 999
        self.test_run.save(update_fields=["testrail_run_id", "updated_at"])
        comment = build_result_comment(
            outcome_label="Passed",
            run_name=self.test_run.display_name,
            result_text=self.pivot_success.result,
            case_url="https://x/case",
        )
        _mark_previously_pushed(self.pivot_success, status_id=1, comment=comment)

        client = MagicMock()
        client.get_run.side_effect = TestRailError(
            "Field :run_id is not a valid id.", 404
        )
        client.add_run.return_value = TestRailRun(
            id=777, name="run", is_completed=False, url=""
        )
        client.add_results_for_cases.return_value = [
            TestRailResult(id=3001, status_id=1),
            TestRailResult(id=3002, status_id=5),
        ]

        outcome = self._push_once(client)

        client.add_run.assert_called_once()
        self.test_run.refresh_from_db()
        self.assertEqual(self.test_run.testrail_run_id, 777)
        self.assertEqual(outcome.testrail_run_id, 777)
        self.assertEqual(outcome.pushed, 2)
        self.assertEqual(outcome.unchanged, 0)

    def test_completed_run_is_recreated(self, _mock_layer: MagicMock) -> None:
        self.test_run.testrail_run_id = 999
        self.test_run.save(update_fields=["testrail_run_id", "updated_at"])

        client = MagicMock()
        client.get_run.return_value = TestRailRun(
            id=999, name="run", is_completed=True, url=""
        )
        client.add_run.return_value = TestRailRun(
            id=888, name="run", is_completed=False, url=""
        )
        client.add_results_for_cases.return_value = [
            TestRailResult(id=4001, status_id=1),
            TestRailResult(id=4002, status_id=5),
        ]

        outcome = self._push_once(client)

        client.add_run.assert_called_once()
        self.assertEqual(outcome.testrail_run_id, 888)

    def test_empty_plan_makes_no_client_calls(self, _mock_layer: MagicMock) -> None:
        self.pivot_success.status = TestRunTestCaseStatus.CANCELLED
        self.pivot_success.save(update_fields=["status", "updated_at"])
        self.pivot_failed.status = TestRunTestCaseStatus.CANCELLED
        self.pivot_failed.save(update_fields=["status", "updated_at"])

        with patch(BUILD_CLIENT_PATH) as mock_build_client:
            outcome = push_test_run_results(
                self.test_run, links_base_url=LINKS_BASE_URL
            )
            mock_build_client.assert_not_called()

        self.assertEqual(
            outcome,
            TestRailPushOutcome(
                pushed=0, unchanged=0, skipped_no_case_id=0, testrail_run_id=0
            ),
        )


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY, CACHES=LOCMEM_CACHE)
@patch(CHANNEL_LAYER_PATH, return_value=None)
class StartTestrailResultsPushTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)
        self.test_run = make_run(project=self.project)

    def _configure_testrail(self) -> None:
        save_testrail_settings(
            project=self.project,
            url="https://a.testrail.com",
            email="a@b.com",
            api_key="k",
        )

    def test_rejects_draft_run(self, _mock_layer: MagicMock) -> None:
        with self.assertRaisesMessage(
            ValueError, "Only finished runs can be pushed to TestRail."
        ):
            start_testrail_results_push(
                test_run=self.test_run, links_base_url=LINKS_BASE_URL
            )

    def test_rejects_run_already_pushing(self, _mock_layer: MagicMock) -> None:
        self.test_run.status = TestRunStatus.DONE
        self.test_run.testrail_push_status = TestRailPushStatus.PUSHING
        self.test_run.save()

        with self.assertRaisesMessage(
            ValueError, "This run is already being pushed to TestRail."
        ):
            start_testrail_results_push(
                test_run=self.test_run, links_base_url=LINKS_BASE_URL
            )

    def test_rejects_missing_testrail_settings(self, _mock_layer: MagicMock) -> None:
        self.test_run.status = TestRunStatus.DONE
        self.test_run.testrail_project_id = 1
        self.test_run.testrail_suite_id = 2
        self.test_run.save()

        with self.assertRaises(TestRailNotConfiguredError):
            start_testrail_results_push(
                test_run=self.test_run, links_base_url=LINKS_BASE_URL
            )

    def test_rejects_missing_target(self, _mock_layer: MagicMock) -> None:
        self._configure_testrail()
        self.test_run.status = TestRunStatus.DONE
        self.test_run.save()

        with self.assertRaisesMessage(
            ValueError, "Choose a TestRail project and suite first."
        ):
            start_testrail_results_push(
                test_run=self.test_run, links_base_url=LINKS_BASE_URL
            )

    def test_success_sets_pushing_and_dispatches_task(
        self, _mock_layer: MagicMock
    ) -> None:
        self._configure_testrail()
        self.test_run.status = TestRunStatus.DONE
        self.test_run.testrail_project_id = 1
        self.test_run.testrail_suite_id = 2
        self.test_run.save()

        with patch(
            "projects.testrail_results_services.celery_app.send_task"
        ) as mock_send_task:
            start_testrail_results_push(
                test_run=self.test_run, links_base_url=LINKS_BASE_URL
            )

        self.test_run.refresh_from_db()
        self.assertEqual(self.test_run.testrail_push_status, TestRailPushStatus.PUSHING)
        mock_send_task.assert_called_once_with(
            "projects.tasks.push_test_run_results",
            args=[self.test_run.id, LINKS_BASE_URL],
        )


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY, CACHES=LOCMEM_CACHE)
@patch(CHANNEL_LAYER_PATH, return_value=None)
class MarkTestrailPushResultTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)
        self.test_run = make_run(project=self.project)

    def test_mark_succeeded_stamps_status_and_summary(
        self, _mock_layer: MagicMock
    ) -> None:
        outcome = TestRailPushOutcome(
            pushed=12, unchanged=3, skipped_no_case_id=1, testrail_run_id=555
        )

        mark_testrail_push_succeeded(self.test_run, outcome)

        self.test_run.refresh_from_db()
        self.assertEqual(self.test_run.testrail_push_status, TestRailPushStatus.PUSHED)
        self.assertIsNotNone(self.test_run.testrail_pushed_at)
        self.assertEqual(self.test_run.testrail_push_error, "")
        self.assertEqual(
            self.test_run.testrail_push_summary,
            "12 results pushed · 3 unchanged · 1 without a TestRail ID",
        )

    def test_mark_succeeded_singular_and_omits_zero_parts(
        self, _mock_layer: MagicMock
    ) -> None:
        outcome = TestRailPushOutcome(
            pushed=1, unchanged=0, skipped_no_case_id=0, testrail_run_id=555
        )

        mark_testrail_push_succeeded(self.test_run, outcome)

        self.test_run.refresh_from_db()
        self.assertEqual(self.test_run.testrail_push_summary, "1 result pushed")

    def test_mark_failed_stamps_status_and_error(self, _mock_layer: MagicMock) -> None:
        mark_testrail_push_failed(self.test_run, "Authentication failed: bad key")

        self.test_run.refresh_from_db()
        self.assertEqual(self.test_run.testrail_push_status, TestRailPushStatus.FAILED)
        self.assertEqual(
            self.test_run.testrail_push_error, "Authentication failed: bad key"
        )


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY, CACHES=LOCMEM_CACHE)
@patch(CHANNEL_LAYER_PATH, return_value=None)
class GetTestrailPushPanelTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)
        self.test_run = make_run(project=self.project)
        self.test_run.status = TestRunStatus.DONE
        self.test_run.save()

    def test_not_configured(self, _mock_layer: MagicMock) -> None:
        panel = get_testrail_push_panel(self.test_run, links_base_url=LINKS_BASE_URL)

        self.assertFalse(panel.configured)
        self.assertFalse(panel.can_push)
        self.assertFalse(panel.has_target)
        self.assertIsNone(panel.picker)
        self.assertIsNone(panel.suggested_project_id)
        self.assertEqual(panel.pending_count, 0)
        self.assertEqual(panel.run_url, "")

    def test_configured_without_target_includes_picker(
        self, _mock_layer: MagicMock
    ) -> None:
        save_testrail_settings(
            project=self.project,
            url="https://a.testrail.com",
            email="a@b.com",
            api_key="k",
        )
        case = make_case(project=self.project)
        case.testrail_id = "10"
        case.save(update_fields=["testrail_id", "updated_at"])
        pivot = add_case_to_run(test_run=self.test_run, test_case=case)
        finish_pivot(pivot, status=TestRunTestCaseStatus.SUCCESS)
        picker_state = TestRailPickerState(configured=True, projects=[])

        with patch(
            "projects.testrail_results_services.get_testrail_picker_state",
            return_value=picker_state,
        ):
            panel = get_testrail_push_panel(
                self.test_run, links_base_url=LINKS_BASE_URL
            )

        self.assertTrue(panel.configured)
        self.assertTrue(panel.can_push)
        self.assertFalse(panel.has_target)
        self.assertIs(panel.picker, picker_state)
        self.assertIsNone(panel.suggested_project_id)
        self.assertEqual(panel.pending_count, 1)
        self.assertEqual(panel.run_url, "")

    def test_with_target_has_no_picker_and_builds_run_url(
        self, _mock_layer: MagicMock
    ) -> None:
        save_testrail_settings(
            project=self.project,
            url="https://a.testrail.com",
            email="a@b.com",
            api_key="k",
        )
        self.test_run.testrail_project_id = 7
        self.test_run.testrail_suite_id = 9
        self.test_run.testrail_run_id = 555
        self.test_run.save()

        panel = get_testrail_push_panel(self.test_run, links_base_url=LINKS_BASE_URL)

        self.assertTrue(panel.has_target)
        self.assertIsNone(panel.picker)
        self.assertEqual(
            panel.run_url, "https://a.testrail.com/index.php?/runs/view/555"
        )


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY, CACHES=LOCMEM_CACHE)
class CanPushTestrailResultsTests(TestCase):
    def test_true_only_for_finished_and_not_pushing(self) -> None:
        user = make_user()
        project = make_project(user=user)
        test_run = make_run(project=project)

        self.assertFalse(can_push_testrail_results(test_run))

        test_run.status = TestRunStatus.DONE
        self.assertTrue(can_push_testrail_results(test_run))

        test_run.testrail_push_status = TestRailPushStatus.PUSHING
        self.assertFalse(can_push_testrail_results(test_run))

        test_run.testrail_push_status = TestRailPushStatus.NOT_PUSHED
        test_run.status = TestRunStatus.CANCELLED
        self.assertTrue(can_push_testrail_results(test_run))
