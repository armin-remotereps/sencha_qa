from __future__ import annotations

from typing import Any

from django.urls import URLPattern, path

from projects import consumers

websocket_urlpatterns: list[URLPattern | Any] = [
    path(
        "ws/projects/<int:project_id>/uploads/",
        consumers.UploadProgressConsumer.as_asgi(),
    ),
    path(
        "ws/projects/<int:project_id>/test-runs/<int:test_run_id>/",
        consumers.TestRunConsumer.as_asgi(),
    ),
    path(
        "ws/projects/<int:project_id>/test-runs/cases/<int:pivot_id>/",
        consumers.TestRunCaseConsumer.as_asgi(),
    ),
]
