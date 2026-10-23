import os

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application
from django.urls import path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "auto_tester.settings")

django_asgi_app = get_asgi_application()

from machine_setup.routing import (  # noqa: E402
    websocket_urlpatterns as machine_setup_websocket_urlpatterns,
)
from projects.controller_consumer import ControllerConsumer  # noqa: E402
from projects.routing import websocket_urlpatterns  # noqa: E402
from visual_engine.routing import (  # noqa: E402
    websocket_urlpatterns as visual_engine_websocket_urlpatterns,
)

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": AuthMiddlewareStack(
            URLRouter(
                [path("ws/controller/", ControllerConsumer.as_asgi())]
                + websocket_urlpatterns
                + machine_setup_websocket_urlpatterns
                + visual_engine_websocket_urlpatterns
            )
        ),
    }
)
