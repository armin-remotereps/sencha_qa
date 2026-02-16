from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from projects.services import (
    CommandResult,
    ControllerActionError,
    controller_run_command,
)


class Command(BaseCommand):
    help = "Run a shell command on a connected controller agent."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--project-id", type=int, required=True, help="Project ID to target"
        )
        parser.add_argument(
            "--command", type=str, required=True, help="Shell command to execute"
        )

    def handle(self, *args: Any, **options: Any) -> None:
        project_id: int = options["project_id"]
        command: str = options["command"]

        try:
            result: CommandResult = controller_run_command(project_id, command=command)
        except ControllerActionError as exc:
            self.stderr.write(f"ERROR: {exc}")
            return

        self.stdout.write(f"success={result['success']}")
        self.stdout.write(f"return_code={result['return_code']}")
        self.stdout.write(f"duration_ms={result['duration_ms']:.1f}")
        self.stdout.write(f"stdout={result['stdout']}")
        self.stdout.write(f"stderr={result['stderr']}")
