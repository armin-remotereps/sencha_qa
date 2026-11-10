from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from projects.exceptions import ControllerActionError
from projects.services import (
    CommandResult,
    controller_run_command,
)
from runners.types import RunnerId


class Command(BaseCommand):
    help = "Run a shell command on a connected controller agent."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--runner-id", type=int, required=True, help="Test Runner ID to target"
        )
        parser.add_argument(
            "--command", type=str, required=True, help="Shell command to execute"
        )

    def handle(self, *args: Any, **options: Any) -> None:
        runner_id = RunnerId(options["runner_id"])
        command: str = options["command"]

        try:
            result: CommandResult = controller_run_command(runner_id, command=command)
        except ControllerActionError as exc:
            self.stderr.write(f"ERROR: {exc}")
            return

        self.stdout.write(f"success={result['success']}")
        self.stdout.write(f"return_code={result['return_code']}")
        self.stdout.write(f"duration_ms={result['duration_ms']:.1f}")
        self.stdout.write(f"stdout={result['stdout']}")
        self.stdout.write(f"stderr={result['stderr']}")
