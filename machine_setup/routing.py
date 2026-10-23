from __future__ import annotations

from typing import Any

from django.urls import URLPattern, path

from machine_setup import consumers

websocket_urlpatterns: list[URLPattern | Any] = [
    path(
        "ws/projects/<int:project_id>/machine-setup/",
        consumers.MachineSetupConsumer.as_asgi(),
    ),
]
