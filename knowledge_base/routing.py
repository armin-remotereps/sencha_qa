from __future__ import annotations

from typing import Any

from channels.security.websocket import AllowedHostsOriginValidator
from django.urls import URLPattern, path

from knowledge_base import consumers

websocket_urlpatterns: list[URLPattern | Any] = [
    path(
        "ws/projects/<int:project_id>/knowledge-base/",
        # Item names and errors of a members-only page: refuse sockets opened
        # from another site with a member's cookie.
        AllowedHostsOriginValidator(consumers.KnowledgeBaseConsumer.as_asgi()),
    ),
]
