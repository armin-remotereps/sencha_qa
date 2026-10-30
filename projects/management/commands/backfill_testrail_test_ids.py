from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from projects.testrail_test_id_backfill_services import backfill_testrail_test_ids


class Command(BaseCommand):
    help = (
        "Fill the TestRail test id of cases imported from a TestRail run before "
        "imports kept it, so Open in TestRail opens the test, not the run."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--project-id", type=int, default=None, help="Only this project"
        )

    def handle(self, *args: Any, **options: Any) -> None:
        project_id: int | None = options["project_id"]
        for result in backfill_testrail_test_ids(project_id):
            line = (
                f"project {result.project_id} ({result.project_name}): "
                f"test cases {result.test_cases_updated}, "
                f"run cases {result.run_cases_updated}"
            )
            if result.missing_runs:
                missing = ", ".join(f"R{run_id}" for run_id in result.missing_runs)
                line += f", runs not found: {missing}"
            if result.error:
                self.stderr.write(f"{line}, stopped: {result.error}")
            else:
                self.stdout.write(line)
