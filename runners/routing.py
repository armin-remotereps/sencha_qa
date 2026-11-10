from __future__ import annotations

from typing import Any

from django.urls import URLPattern, path

from runners import consumers

websocket_urlpatterns: list[URLPattern | Any] = [
    path("ws/runners/status/", consumers.OwnedRunnersStatusConsumer.as_asgi()),
    path(
        "ws/runners/<int:runner_id>/status/",
        consumers.RunnerStatusConsumer.as_asgi(),
    ),
]
