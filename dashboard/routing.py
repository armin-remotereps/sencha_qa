from __future__ import annotations

from typing import Any

from channels.security.websocket import AllowedHostsOriginValidator
from django.urls import URLPattern, path

from dashboard import consumers

websocket_urlpatterns: list[URLPattern | Any] = [
    path(
        "ws/attention/",
        # Opened by every app page for its own user: refuse sockets opened from
        # another site with the user's cookie.
        AllowedHostsOriginValidator(consumers.AttentionConsumer.as_asgi()),
    ),
]
