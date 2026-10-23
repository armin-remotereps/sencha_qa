from __future__ import annotations

from typing import Any

from django.urls import URLPattern, path

from visual_engine.consumers import VisionWorkerConsumer

websocket_urlpatterns: list[URLPattern | Any] = [
    path("ws/vision-worker/", VisionWorkerConsumer.as_asgi()),
]
