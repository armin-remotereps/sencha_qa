from __future__ import annotations

from django.test import TestCase

from controller_client.protocol import OmniParserState
from projects.models import TestRunStatus
from projects.services import get_run_readiness
from projects.tests.helpers import make_case, make_project, make_run, make_user


class GetRunReadinessTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)

    def test_blocks_when_no_cases_selected(self) -> None:
        readiness = get_run_readiness(self.project, selected_count=0)
        blocker_keys = {check.key for check in readiness.blockers}
        self.assertIn("cases_selected", blocker_keys)
        self.assertFalse(readiness.ready)

    def test_passes_cases_selected_when_count_given(self) -> None:
        readiness = get_run_readiness(self.project, selected_count=3)
        check = next(c for c in readiness.checks if c.key == "cases_selected")
        self.assertTrue(check.passed)

    def test_cases_selected_falls_back_to_existing_run_pivots(self) -> None:
        test_run = make_run(project=self.project)
        case = make_case(project=self.project)
        test_run.pivot_entries.create(test_case=case)

        readiness = get_run_readiness(self.project, test_run=test_run)

        check = next(c for c in readiness.checks if c.key == "cases_selected")
        self.assertTrue(check.passed)

    def test_blocks_when_machine_disconnected(self) -> None:
        readiness = get_run_readiness(self.project, selected_count=1)
        check = next(c for c in readiness.checks if c.key == "machine_connected")
        self.assertFalse(check.passed)
        self.assertTrue(check.blocking)

    def test_passes_machine_and_visual_engine_when_ready(self) -> None:
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

        readiness = get_run_readiness(self.project, selected_count=1)

        machine_check = next(
            c for c in readiness.checks if c.key == "machine_connected"
        )
        engine_check = next(
            c for c in readiness.checks if c.key == "visual_engine_ready"
        )
        self.assertTrue(machine_check.passed)
        self.assertTrue(engine_check.passed)

    def test_no_active_run_ignores_the_run_itself(self) -> None:
        test_run = make_run(project=self.project)
        test_run.status = TestRunStatus.STARTED
        test_run.save()

        readiness = get_run_readiness(self.project, test_run=test_run, selected_count=1)

        check = next(c for c in readiness.checks if c.key == "no_active_run")
        self.assertTrue(check.passed)

    def test_no_active_run_blocks_when_another_run_is_active(self) -> None:
        other_run = make_run(project=self.project)
        other_run.status = TestRunStatus.STARTED
        other_run.save()

        readiness = get_run_readiness(self.project, selected_count=1)

        check = next(c for c in readiness.checks if c.key == "no_active_run")
        self.assertFalse(check.passed)

    def test_application_context_is_advisory_not_blocking(self) -> None:
        readiness = get_run_readiness(self.project, selected_count=1)
        check = next(c for c in readiness.checks if c.key == "application_context")
        self.assertFalse(check.passed)
        self.assertFalse(check.blocking)
        self.assertNotIn(check, readiness.blockers)

    def test_cases_selected_title_reads_as_a_problem_when_failed(self) -> None:
        readiness = get_run_readiness(self.project, selected_count=0)
        check = next(c for c in readiness.checks if c.key == "cases_selected")
        self.assertEqual(check.title, "No test cases selected")
        self.assertEqual(check.help, "Select at least one test case to run.")

    def test_cases_selected_title_pluralizes_when_passed(self) -> None:
        readiness = get_run_readiness(self.project, selected_count=1)
        check = next(c for c in readiness.checks if c.key == "cases_selected")
        self.assertEqual(check.title, "1 test case selected")
        self.assertEqual(check.help, "")

        readiness = get_run_readiness(self.project, selected_count=3)
        check = next(c for c in readiness.checks if c.key == "cases_selected")
        self.assertEqual(check.title, "3 test cases selected")
        self.assertEqual(check.help, "")

    def test_machine_connected_title_reads_as_a_problem_when_failed(self) -> None:
        readiness = get_run_readiness(self.project, selected_count=1)
        check = next(c for c in readiness.checks if c.key == "machine_connected")
        self.assertEqual(check.title, "Test machine not connected")
        self.assertNotEqual(check.help, "")

    def test_no_active_run_title_reads_as_a_problem_when_failed(self) -> None:
        other_run = make_run(project=self.project)
        other_run.status = TestRunStatus.STARTED
        other_run.save()

        readiness = get_run_readiness(self.project, selected_count=1)

        check = next(c for c in readiness.checks if c.key == "no_active_run")
        self.assertEqual(check.title, "Another run is in progress")
        self.assertNotEqual(check.help, "")

    def test_passed_checks_read_as_positive_statements(self) -> None:
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

        readiness = get_run_readiness(self.project, selected_count=1)

        machine_check = next(
            c for c in readiness.checks if c.key == "machine_connected"
        )
        engine_check = next(
            c for c in readiness.checks if c.key == "visual_engine_ready"
        )
        active_run_check = next(c for c in readiness.checks if c.key == "no_active_run")
        self.assertEqual(machine_check.title, "Test machine connected")
        self.assertEqual(machine_check.help, "")
        self.assertEqual(engine_check.title, "Visual engine ready")
        self.assertEqual(engine_check.help, "")
        self.assertEqual(active_run_check.title, "No other run active")
        self.assertEqual(active_run_check.help, "")

    def test_application_context_title_reads_as_positive_when_provided(self) -> None:
        self.project.application_url = "https://example.com"
        self.project.save()

        readiness = get_run_readiness(self.project, selected_count=1)

        check = next(c for c in readiness.checks if c.key == "application_context")
        self.assertEqual(check.title, "Application context provided")
        self.assertEqual(check.help, "")
