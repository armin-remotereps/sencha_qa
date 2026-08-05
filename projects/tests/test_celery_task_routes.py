from __future__ import annotations

from django.conf import settings
from django.test import SimpleTestCase

from auto_tester.celery import app


class CeleryTaskRoutesTests(SimpleTestCase):
    def test_every_project_task_has_an_explicit_queue_route(self) -> None:
        """A task with no CELERY_TASK_ROUTES entry silently falls to Celery's
        default 'celery' queue, which no documented dev worker consumes --
        it just sits there forever with no error. Every projects.tasks.* task
        must be routed explicitly."""
        app.loader.import_default_modules()
        project_tasks = [
            name for name in app.tasks if name.startswith("projects.tasks.")
        ]

        self.assertTrue(project_tasks, "expected at least one registered task")

        unrouted = [
            name for name in project_tasks if name not in settings.CELERY_TASK_ROUTES
        ]
        self.assertEqual(
            unrouted,
            [],
            f"tasks missing a CELERY_TASK_ROUTES entry (will silently stall "
            f"in the default queue): {unrouted}",
        )
