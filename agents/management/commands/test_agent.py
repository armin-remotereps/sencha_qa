from typing import Any

from django.core.management.base import BaseCommand

from projects.services import execute_test_run_test_case


class Command(BaseCommand):
    def handle(self, *args: Any, **options: Any) -> None:
        execute_test_run_test_case(5)
