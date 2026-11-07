"""ASGI entrypoint of the MCP gateway (compose service ``mcp``, uvicorn).

Runs apart from Daphne: the MCP SDK needs the ASGI lifespan, and large JSON
bodies and slow tool calls stay off the process holding runner WebSockets.

The Starlette app returned by ``streamable_http_app`` already owns the
lifespan (it enters ``session_manager.run()`` exactly once), so lifespan
events are passed through untouched; this module only adds the body cap and
publishes the client IP (``scope["client"]``, the real IP behind nginx with
``--proxy-headers``) for the key check's per-IP failure throttle.
"""

from __future__ import annotations

import os

import django
from django.conf import settings
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "auto_tester.settings")

# The gateway imports models; the app registry must be ready first, as in
# auto_tester/asgi.py (get_asgi_application() does the same setup there).
django.setup()

from mcp_gateway.limits import MCP_REQUEST_MAX_BYTES  # noqa: E402
from mcp_gateway.mcp_app import build_mcp_server  # noqa: E402
from mcp_gateway.services.authenticate_project_key import (  # noqa: E402
    MCP_CLIENT_IP,
)

MCP_PATH = "/mcp"


class RequestBodyCap:
    """Refuse unauthenticated or oversized HTTP requests before parsing.

    A request without an ``Authorization: Bearer <token>`` header (non-empty
    token) is answered 401 before its body is read; the key itself is
    checked later by the SDK's bearer middleware. A declared
    ``Content-Length`` over ``max_bytes`` is refused with 413 without reading
    the body. Otherwise the body is read here, counting streamed bytes
    (chunked or understated bodies), and refused as soon as it passes the
    cap; a body within the cap is replayed to the app as one message and
    this middleware drops its reference at once. Peak memory per request is
    therefore about twice the body for the moment the chunks are joined
    (once for a body that arrived in one chunk), then one copy owned by the
    SDK, which buffers the whole JSON body anyway.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Lifespan passes through: the Starlette app enters
            # session_manager.run() itself.
            await self.app(scope, receive, send)
            return

        # Read by the SDK's auth middleware in this same task (contextvar).
        client = scope.get("client")
        MCP_CLIENT_IP.set(client[0] if client else "")

        declared: int | None = None
        has_bearer = False
        for name, value in scope["headers"]:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    await PlainTextResponse("Invalid Content-Length", status_code=400)(
                        scope, receive, send
                    )
                    return
            elif name == b"authorization":
                has_bearer = _is_bearer(value)
        if not has_bearer:
            await _unauthorized(scope, receive, send)
            return
        if declared is not None and declared > self.max_bytes:
            await _too_large(scope, receive, send)
            return

        chunks: list[bytes] = []
        received = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # Client went away before the body was complete.
                return
            chunk = message.get("body", b"")
            received += len(chunk)
            if received > self.max_bytes:
                chunks.clear()
                await _too_large(scope, receive, send)
                return
            if chunk:
                chunks.append(chunk)
            if not message.get("more_body", False):
                break

        async def replay_receive() -> Message:
            if chunks:
                body = b"".join(chunks)
                # Drop this middleware's reference: only the app's copy stays.
                chunks.clear()
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)


def _is_bearer(value: bytes) -> bool:
    """``Bearer <non-empty token>`` (scheme case-insensitive, as the SDK)."""
    scheme, _, token = value.partition(b" ")
    return scheme.lower() == b"bearer" and bool(token.strip())


async def _unauthorized(scope: Scope, receive: Receive, send: Send) -> None:
    # The shape of the SDK's own bearer middleware refusal.
    description = "Authentication required"
    await JSONResponse(
        {"error": "invalid_token", "error_description": description},
        status_code=401,
        headers={
            "WWW-Authenticate": (
                f'Bearer error="invalid_token", error_description="{description}"'
            )
        },
    )(scope, receive, send)


async def _too_large(scope: Scope, receive: Receive, send: Send) -> None:
    await PlainTextResponse("Request body too large", status_code=413)(
        scope, receive, send
    )


def _transport_security() -> TransportSecuritySettings:
    """DNS rebinding protection from Django's ``ALLOWED_HOSTS``.

    ``*`` disables the Host/Origin check (as it does for Django); a leading
    dot (``.example.com``) cannot be expressed by the SDK's matcher, so it is
    allowed as the bare domain only.
    """
    hosts = [host.lstrip(".") for host in settings.ALLOWED_HOSTS if host]
    if "*" in hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*hosts, *(f"{host}:*" for host in hosts)],
        allowed_origins=[
            origin
            for host in hosts
            for origin in (
                f"https://{host}",
                f"https://{host}:*",
                f"http://{host}",
                f"http://{host}:*",
            )
        ],
    )


def _build_application() -> ASGIApp:
    server = build_mcp_server()
    starlette_app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        max_request_body_size=MCP_REQUEST_MAX_BYTES,
        transport_security=_transport_security(),
        host="0.0.0.0",
    )
    return RequestBodyCap(starlette_app, MCP_REQUEST_MAX_BYTES)


application = _build_application()
