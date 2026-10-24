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
    path(
        # One thread (an auto-fix thread, or the main chat by id): same group
        # and events; current_state loads that thread.
        "ws/projects/<int:project_id>/machine-setup/threads/<int:conversation_id>/",
        AllowedHostsOriginValidator(consumers.MachineSetupConsumer.as_asgi()),
    ),
]
