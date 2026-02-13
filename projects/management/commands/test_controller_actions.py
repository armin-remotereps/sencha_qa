from __future__ import annotations

import sys
from typing import Any

from django.core.management.base import BaseCommand

from projects.services import (
    ActionResult,
    ControllerActionError,
    ScreenshotResult,
    controller_click,
    controller_drag,
    controller_hover,
    controller_key_press,
    controller_screenshot,
    controller_type_text,
)


class Command(BaseCommand):
    help = "Test all 6 controller actions against a connected agent."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--project-id", type=int, required=True, help="Project ID to test against"
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=30.0,
            help="Per-action timeout in seconds (default: 30)",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        project_id: int = options["project_id"]
        timeout: float = options["timeout"]

        results: list[tuple[str, bool, str]] = []

        results.append(self._test_click(project_id, timeout))
        results.append(self._test_hover(project_id, timeout))
        results.append(self._test_drag(project_id, timeout))
        results.append(self._test_type_text(project_id, timeout))
        results.append(self._test_key_press(project_id, timeout))
        results.append(self._test_screenshot(project_id, timeout))

        self.stdout.write("\n--- Summary ---")
        passed = 0
        failed = 0
        for name, success, detail in results:
            status = "PASS" if success else "FAIL"
            if success:
                passed += 1
            else:
                failed += 1
            self.stdout.write(f"  {status}: {name} - {detail}")

        self.stdout.write(f"\nTotal: {passed} passed, {failed} failed")
        if failed > 0:
            sys.exit(1)

    def _test_click(self, project_id: int, timeout: float) -> tuple[str, bool, str]:
        name = "click"
        try:
            result: ActionResult = controller_click(
                project_id, x=100, y=200, button="left", timeout=timeout
            )
            self.stdout.write(
                f"[click] success={result['success']} msg={result['message']}"
            )
            return name, result["success"], result["message"]
        except ControllerActionError as exc:
            self.stdout.write(f"[click] ERROR: {exc}")
            return name, False, str(exc)

    def _test_hover(self, project_id: int, timeout: float) -> tuple[str, bool, str]:
        name = "hover"
        try:
            result: ActionResult = controller_hover(
                project_id, x=150, y=250, timeout=timeout
            )
            self.stdout.write(
                f"[hover] success={result['success']} msg={result['message']}"
            )
            return name, result["success"], result["message"]
        except ControllerActionError as exc:
            self.stdout.write(f"[hover] ERROR: {exc}")
            return name, False, str(exc)

    def _test_drag(self, project_id: int, timeout: float) -> tuple[str, bool, str]:
        name = "drag"
        try:
            result: ActionResult = controller_drag(
                project_id,
                start_x=100,
                start_y=100,
                end_x=300,
                end_y=300,
                button="left",
                duration=0.5,
                timeout=timeout,
            )
            self.stdout.write(
                f"[drag] success={result['success']} msg={result['message']}"
            )
            return name, result["success"], result["message"]
        except ControllerActionError as exc:
            self.stdout.write(f"[drag] ERROR: {exc}")
            return name, False, str(exc)

    def _test_type_text(self, project_id: int, timeout: float) -> tuple[str, bool, str]:
        name = "type_text"
        try:
            result: ActionResult = controller_type_text(
                project_id, text="hello world", interval=0.0, timeout=timeout
            )
            self.stdout.write(
                f"[type_text] success={result['success']} msg={result['message']}"
            )
            return name, result["success"], result["message"]
        except ControllerActionError as exc:
            self.stdout.write(f"[type_text] ERROR: {exc}")
            return name, False, str(exc)

    def _test_key_press(self, project_id: int, timeout: float) -> tuple[str, bool, str]:
        name = "key_press"
        try:
            result: ActionResult = controller_key_press(
                project_id, keys="enter", timeout=timeout
            )
            self.stdout.write(
                f"[key_press] success={result['success']} msg={result['message']}"
            )
            return name, result["success"], result["message"]
        except ControllerActionError as exc:
            self.stdout.write(f"[key_press] ERROR: {exc}")
            return name, False, str(exc)

    def _test_screenshot(
        self, project_id: int, timeout: float
    ) -> tuple[str, bool, str]:
        name = "screenshot"
        try:
            result: ScreenshotResult = controller_screenshot(
                project_id, timeout=timeout
            )
            detail = (
                f"success={result['success']} "
                f"size={result['width']}x{result['height']} "
                f"format={result['format']} "
                f"data_len={len(result['image_base64'])}"
            )
            self.stdout.write(f"[screenshot] {detail}")
            return name, result["success"], detail
        except ControllerActionError as exc:
            self.stdout.write(f"[screenshot] ERROR: {exc}")
            return name, False, str(exc)
