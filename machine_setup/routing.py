from __future__ import annotations

from typing import Any

from channels.security.websocket import AllowedHostsOriginValidator
from django.urls import URLPattern, path

from machine_setup import consumers

websocket_urlpatterns: list[URLPattern | Any] = [
    path(
        "ws/projects/<int:project_id>/machine-setup/",
        # The chat streams command output and screenshots: refuse sockets
        # opened from another site with a member's cookie. Not applied to the
        # whole stack, because the Test Runner's socket sends no Origin.
        AllowedHostsOriginValidator(consumers.MachineSetupConsumer.as_asgi()),
    ),
]
