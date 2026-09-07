from __future__ import annotations

from django.test import TestCase

from controller_client.protocol import OmniParserState
from projects.models import ApplicationPlatform, TestRunStatus, TestRunTestCaseStatus
from projects.services import (
    MachineState,
    PrimaryAction,
    build_agent_project_context,
    get_latest_runs_by_project,
    get_machine_state,
    get_project_overview,
    list_projects_for_user,
    resolve_primary_action,
    save_application_context,
)
from projects.tests.helpers import make_case, make_project, make_run, make_user


class BuildAgentProjectContextTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)

    def test_returns_none_when_nothing_is_set(self) -> None:
        self.assertIsNone(build_agent_project_context(self.project))

    def test_combines_url_platform_and_notes(self) -> None:
        self.project.application_url = "https://app.example.com"
        self.project.application_platform = ApplicationPlatform.WEB
        self.project.project_prompt = "Log in with test@example.com."
        self.project.save()

        context = build_agent_project_context(self.project)

        assert context is not None
        self.assertIn("https://app.example.com", context)
        self.assertIn("Web application", context)
        self.assertIn("Log in with test@example.com.", context)


class SaveApplicationContextTests(TestCase):
    def test_persists_all_three_fields(self) -> None:
        user = make_user()
        project = make_project(user=user)

        save_application_context(
            project=project,
            application_url="https://app.example.com",
            application_platform=ApplicationPlatform.WINDOWS,
            project_prompt="Some notes.",
        )

        project.refresh_from_db()
        self.assertEqual(project.application_url, "https://app.example.com")
        self.assertEqual(project.application_platform, ApplicationPlatform.WINDOWS)
        self.assertEqual(project.project_prompt, "Some notes.")
        self.assertTrue(project.has_application_context)


class ListProjectsForUserArchivedTests(TestCase):
    def test_excludes_archived_by_default(self) -> None:
        user = make_user()
        active = make_project(user=user, name="Active")
        archived = make_project(user=user, name="Archived")
        archived.archived = True
        archived.save()

        page = list_projects_for_user(
            user=user, search=None, tag_filter=None, page=1, per_page=20
        )

        names = {project.name for project in page.object_list}
        self.assertIn(active.name, names)
        self.assertNotIn(archived.name, names)

    def test_include_archived_returns_both(self) -> None:
        user = make_user()
        active = make_project(user=user, name="Active")
        archived = make_project(user=user, name="Archived")
        archived.archived = True
        archived.save()

        page = list_projects_for_user(
            user=user,
            search=None,
            tag_filter=None,
            page=1,
            per_page=20,
            include_archived=True,
        )

        names = {project.name for project in page.object_list}
        self.assertIn(active.name, names)
        self.assertIn(archived.name, names)


class ListProjectsForUserAnnotationsTests(TestCase):
    def test_member_and_case_counts_are_not_inflated_by_the_join_fan_out(self) -> None:
        user = make_user()
        other_member = make_user(email="second@example.com")
        project = make_project(user=user)
        project.members.add(other_member)
        make_case(project=project, title="Case one")
        make_case(project=project, title="Case two")
        make_case(project=project, title="Case three")

        page = list_projects_for_user(
            user=user, search=None, tag_filter=None, page=1, per_page=20
        )

        (result,) = page.object_list
        self.assertEqual(result.member_count, 2)
        self.assertEqual(result.case_count, 3)

    def test_latest_run_id_is_the_most_recently_created_run(self) -> None:
        user = make_user()
        project = make_project(user=user)
        make_run(project=project, name="First")
        newest = make_run(project=project, name="Second")

        page = list_projects_for_user(
            user=user, search=None, tag_filter=None, page=1, per_page=20
        )

        (result,) = page.object_list
        self.assertEqual(result.latest_run_id, newest.id)

    def test_latest_run_id_is_none_without_runs(self) -> None:
        user = make_user()
        make_project(user=user)

        page = list_projects_for_user(
            user=user, search=None, tag_filter=None, page=1, per_page=20
        )

        (result,) = page.object_list
        self.assertIsNone(result.latest_run_id)


class GetLatestRunsByProjectTests(TestCase):
    def test_returns_one_run_per_project_keyed_by_project_id(self) -> None:
        user = make_user()
        project = make_project(user=user)
        case = make_case(project=project)
        run = make_run(project=project)
        pivot = run.pivot_entries.create(test_case=case)
        pivot.status = TestRunTestCaseStatus.SUCCESS
        pivot.save(update_fields=["status"])

        page = list_projects_for_user(
            user=user, search=None, tag_filter=None, page=1, per_page=20
        )

        latest_runs = get_latest_runs_by_project(page.object_list)

        self.assertEqual(set(latest_runs), {project.id})
        self.assertEqual(latest_runs[project.id].id, run.id)
        self.assertEqual(latest_runs[project.id].success_count, 1)  # type: ignore[attr-defined]  # annotate() field
        self.assertEqual(latest_runs[project.id].failed_count, 0)  # type: ignore[attr-defined]  # annotate() field

    def test_projects_without_runs_are_absent_from_the_result(self) -> None:
        user = make_user()
        make_project(user=user)

        page = list_projects_for_user(
            user=user, search=None, tag_filter=None, page=1, per_page=20
        )

        self.assertEqual(get_latest_runs_by_project(page.object_list), {})


class GetMachineStateTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)

    def test_disconnected_when_agent_not_connected(self) -> None:
        self.assertEqual(get_machine_state(self.project), MachineState.DISCONNECTED)

    def test_starting_when_connected_with_empty_status(self) -> None:
        self.project.agent_connected = True
        self.project.save()
        self.assertEqual(get_machine_state(self.project), MachineState.STARTING)

    def test_ready_when_omniparser_reports_ready(self) -> None:
        self.project.agent_connected = True
        self.project.agent_omniparser_status = {
            "state": OmniParserState.READY.value,
            "message": "",
            "device": "cpu",
            "weights_dir": "",
            "phase": "ready",
            "load_seconds": 1.0,
        }
        self.project.save()
        self.assertEqual(get_machine_state(self.project), MachineState.READY)

    def test_failed_when_omniparser_reports_failed(self) -> None:
        self.project.agent_connected = True
        self.project.agent_omniparser_status = {
            "state": OmniParserState.FAILED.value,
            "message": "boom",
            "device": "cpu",
            "weights_dir": "",
            "phase": "load",
            "load_seconds": 1.0,
        }
        self.project.save()
        self.assertEqual(get_machine_state(self.project), MachineState.FAILED)


class ResolvePrimaryActionTests(TestCase):
    def test_import_cases_when_no_cases(self) -> None:
        action = resolve_primary_action(
            case_count=0,
            machine_state=MachineState.READY,
            active_run=None,
            latest_run=None,
        )
        self.assertEqual(action, PrimaryAction.IMPORT_CASES)

    def test_connect_machine_when_disconnected(self) -> None:
        action = resolve_primary_action(
            case_count=5,
            machine_state=MachineState.DISCONNECTED,
            active_run=None,
            latest_run=None,
        )
        self.assertEqual(action, PrimaryAction.CONNECT_MACHINE)

    def test_create_run_when_no_history(self) -> None:
        action = resolve_primary_action(
            case_count=5,
            machine_state=MachineState.READY,
            active_run=None,
            latest_run=None,
        )
        self.assertEqual(action, PrimaryAction.CREATE_RUN)


class GetProjectOverviewTests(TestCase):
    def test_empty_project_overview(self) -> None:
        user = make_user()
        project = make_project(user=user)

        overview = get_project_overview(project)

        self.assertEqual(overview.case_count, 0)
        self.assertEqual(overview.never_run_count, 0)
        self.assertEqual(overview.machine_state, MachineState.DISCONNECTED)
        self.assertIsNone(overview.active_run)
        self.assertIsNone(overview.latest_run)
        self.assertIsNone(overview.latest_run_summary)
        self.assertEqual(overview.draft_run_count, 0)
        self.assertFalse(overview.has_application_context)
        self.assertEqual(overview.primary_action, PrimaryAction.IMPORT_CASES)
        self.assertIsNone(overview.pass_rate_30d)
        self.assertEqual(overview.recent_runs, [])

    def test_never_run_count_excludes_cases_with_run_history(self) -> None:
        user = make_user()
        project = make_project(user=user)
        make_case(project=project, title="Never run")
        run_case = make_case(project=project, title="Has a run")
        test_run = make_run(project=project)
        test_run.pivot_entries.create(test_case=run_case)

        overview = get_project_overview(project)

        self.assertEqual(overview.case_count, 2)
        self.assertEqual(overview.never_run_count, 1)

    def test_draft_run_count_and_latest_run(self) -> None:
        user = make_user()
        project = make_project(user=user)
        make_run(project=project, name="Draft one")
        make_run(project=project, name="Draft two")

        overview = get_project_overview(project)

        self.assertEqual(overview.draft_run_count, 2)
        self.assertIsNotNone(overview.latest_run)
        self.assertEqual(overview.latest_run.name, "Draft two")  # type: ignore[union-attr]
        self.assertEqual(overview.primary_action, PrimaryAction.IMPORT_CASES)

    def test_active_run_is_detected(self) -> None:
        user = make_user()
        project = make_project(user=user)
        make_case(project=project)
        active_run = make_run(project=project, name="Active")
        active_run.status = TestRunStatus.STARTED
        active_run.save()

        overview = get_project_overview(project)

        self.assertIsNotNone(overview.active_run)
        self.assertEqual(overview.active_run.id, active_run.id)  # type: ignore[union-attr]

    def test_recent_runs_are_annotated_and_capped_at_five(self) -> None:
        user = make_user()
        project = make_project(user=user)
        case = make_case(project=project)
        runs = [make_run(project=project, name=f"Run {i}") for i in range(6)]
        runs[-1].pivot_entries.create(test_case=case)

        overview = get_project_overview(project)

        self.assertEqual(len(overview.recent_runs), 5)
        self.assertEqual(overview.recent_runs[0].id, runs[-1].id)
        self.assertEqual(overview.recent_runs[0].case_count, 1)  # type: ignore[attr-defined]  # annotate() field
