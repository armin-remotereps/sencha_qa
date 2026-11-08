"""ASGI entrypoint of the MCP gateway (compose service ``mcp``, uvicorn).

Runs apart from Daphne: the MCP SDK needs the ASGI lifespan, and large JSON
bodies and slow tool calls stay off the process holding runner WebSockets.

``GatewayDispatcher`` routes by path:

- ``/mcp``: the MCP Starlette app behind the bearer pre-check and the 10 MiB
  body cap (``RequestBodyCap``).
- the OAuth endpoints (``mcp_gateway.oauth_app.OAUTH_PATHS``, only while
  ``MCP_OAUTH_ENABLED``): per-IP (and for ``/register`` global) throttles,
  then the OAuth Starlette app behind the 64 KiB cap. Browsers' ``Origin``
  there is handled by the SDK's CORS wrapping; the Host/Origin transport
  security below applies to ``/mcp`` only.
- anything else (``/.well-known/oauth-protected-resource/mcp``, 404s): the
  MCP Starlette app with the 64 KiB cap and no bearer pre-check.

The MCP Starlette app returned by ``streamable_http_app`` owns the lifespan
(it enters ``session_manager.run()`` exactly once), so lifespan events go to
it alone, untouched (the dispatcher only sets the loop's bounded default
executor, ``MCP_EXECUTOR_THREADS``, before passing it on); the OAuth app has
no lifespan. The dispatcher also
publishes the client IP (``scope["client"]``, the real IP behind nginx with
``--proxy-headers``) for the bearer check's per-IP failure counter and the
OAuth throttles.
"""

from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Final

import django
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.cache import cache
from mcp.server.auth.routes import (
    AUTHORIZATION_PATH,
    REGISTRATION_PATH,
    REVOCATION_PATH,
    TOKEN_PATH,
)
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "auto_tester.settings")

# The gateway imports models; the app registry must be ready first, as in
# auto_tester/asgi.py (get_asgi_application() does the same setup there).
django.setup()

from mcp_gateway.limits import (  # noqa: E402
    MCP_EXECUTOR_THREADS,
    MCP_OAUTH_AUTHORIZE_PER_MINUTE,
    MCP_OAUTH_REGISTER_PER_HOUR_GLOBAL,
    MCP_OAUTH_REGISTER_PER_HOUR_PER_IP,
    MCP_OAUTH_REGISTER_PER_MINUTE,
    MCP_OAUTH_REQUEST_MAX_BYTES,
    MCP_OAUTH_REVOKE_PER_MINUTE,
    MCP_OAUTH_TOKEN_PER_MINUTE,
    MCP_REQUEST_MAX_BYTES,
)
from mcp_gateway.mcp_app import build_mcp_server  # noqa: E402
from mcp_gateway.oauth_app import OAUTH_PATHS, build_oauth_app  # noqa: E402
from mcp_gateway.oauth_policy import (  # noqa: E402
    MCP_PATH,
    MCP_SCOPE,
    oauth_enabled,
    resource_metadata_url,
)
from mcp_gateway.oauth_provider import DjangoOAuthProvider  # noqa: E402
from mcp_gateway.services.authenticate_bearer import MCP_CLIENT_IP  # noqa: E402
from projects.runner_person_step_services import hit_runner_throttle  # noqa: E402

logger = logging.getLogger(__name__)

# Requests per client IP and minute, by OAuth path (metadata is not counted).
_OAUTH_PER_IP_PER_MINUTE: Final[dict[str, int]] = {
    REGISTRATION_PATH: MCP_OAUTH_REGISTER_PER_MINUTE,
    AUTHORIZATION_PATH: MCP_OAUTH_AUTHORIZE_PER_MINUTE,
    TOKEN_PATH: MCP_OAUTH_TOKEN_PER_MINUTE,
    REVOCATION_PATH: MCP_OAUTH_REVOKE_PER_MINUTE,
}
_MINUTE: Final = 60
_HOUR: Final = 3600


class RequestBodyCap:
    """Refuse unauthenticated or oversized HTTP requests before parsing.

    With a ``bearer_challenge`` (``/mcp``), a request without an
    ``Authorization: Bearer <token>`` header (non-empty token) is answered
    401 with that ``WWW-Authenticate`` value before its body is read; the
    token itself is checked later by the SDK's bearer middleware. A declared
    ``Content-Length`` over ``max_bytes`` is refused with 413 without reading
    the body. Otherwise the body is read here, counting streamed bytes
    (chunked or understated bodies), and refused as soon as it passes the
    cap; a body within the cap is replayed to the app as one message and
    this middleware drops its reference at once. Peak memory per request is
    therefore about twice the body for the moment the chunks are joined
    (once for a body that arrived in one chunk), then one copy owned by the
    SDK, which buffers the whole JSON body anyway.
    """

    def __init__(
        self, app: ASGIApp, max_bytes: int, *, bearer_challenge: str | None
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.bearer_challenge = bearer_challenge

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
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
        if self.bearer_challenge is not None and not has_bearer:
            await _unauthorized(self.bearer_challenge, scope, receive, send)
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

        replayed = False

        async def replay_receive() -> Message:
            # The body was already read from the client, so it is replayed as
            # exactly one message, even when empty (a GET /authorize): falling
            # through to ``receive()`` for it would wait for the disconnect.
            nonlocal replayed
            if not replayed:
                replayed = True
                body = b"".join(chunks)
                # Drop this middleware's reference: only the app's copy stays.
                chunks.clear()
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)


class GatewayDispatcher:
    """Route each request by path (see the module docstring)."""

    def __init__(self, mcp_starlette: ASGIApp, oauth_starlette: ASGIApp | None) -> None:
        self.mcp_starlette = mcp_starlette
        self.mcp_endpoint = RequestBodyCap(
            mcp_starlette, MCP_REQUEST_MAX_BYTES, bearer_challenge=_bearer_challenge()
        )
        self.other = RequestBodyCap(
            mcp_starlette, MCP_OAUTH_REQUEST_MAX_BYTES, bearer_challenge=None
        )
        self.oauth = (
            RequestBodyCap(
                oauth_starlette, MCP_OAUTH_REQUEST_MAX_BYTES, bearer_challenge=None
            )
            if oauth_starlette is not None
            else None
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "lifespan":
                _set_default_executor()
            # Lifespan goes to the MCP Starlette app alone: it enters
            # session_manager.run() itself.
            await self.mcp_starlette(scope, receive, send)
            return

        # Read by the SDK's auth middleware in this same task (contextvar).
        client = scope.get("client")
        ip = client[0] if client else ""
        MCP_CLIENT_IP.set(ip)

        path = scope["path"]
        # nginx's ``location /mcp`` also passes ``/mcp/`` (the SDK redirects it).
        if path in (MCP_PATH, MCP_PATH + "/"):
            await self.mcp_endpoint(scope, receive, send)
        elif self.oauth is not None and path in OAUTH_PATHS:
            if not await sync_to_async(_oauth_request_allowed, thread_sensitive=False)(
                path, ip
            ):
                await _throttled(scope, receive, send)
                return
            await self.oauth(scope, receive, send)
        else:
            await self.other(scope, receive, send)


def _set_default_executor() -> None:
    """Give the event loop an explicit, bounded default executor (once per
    process, at lifespan startup): tool calls run their sync work there
    (``sync_to_async(thread_sensitive=False)``), and the default size
    depends on the host's CPU count."""
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(
            max_workers=MCP_EXECUTOR_THREADS, thread_name_prefix="mcp-tool"
        )
    )


def _oauth_request_allowed(path: str, ip: str) -> bool:
    """Count one request to an OAuth ``path``; False once over a limit."""
    per_ip = _OAUTH_PER_IP_PER_MINUTE.get(path)
    if per_ip is None:
        return True
    name = path.strip("/")
    if ip and not _counted(f"mcp:oauth:{name}:{ip}", per_ip, _MINUTE, name, "per-IP"):
        return False
    if path != REGISTRATION_PATH:
        return True
    if ip and not _counted(
        f"mcp:oauth:register-hour:{ip}",
        MCP_OAUTH_REGISTER_PER_HOUR_PER_IP,
        _HOUR,
        name,
        "per-IP hourly",
    ):
        return False
    return _counted(
        "mcp:oauth:register:global",
        MCP_OAUTH_REGISTER_PER_HOUR_GLOBAL,
        _HOUR,
        name,
        "global hourly",
    )


def _counted(key: str, limit: int, window: int, name: str, kind: str) -> bool:
    """One hit under ``key``; no write once over ``limit``, and one warning
    when the window crosses it."""
    attempts = cache.get(key)
    if isinstance(attempts, int) and attempts > limit:
        return False
    if hit_runner_throttle(key, limit=limit, window_seconds=window):
        return True
    logger.warning("mcp oauth %s requests over the %s limit", name, kind)
    return False


def _bearer_challenge() -> str:
    """The ``WWW-Authenticate`` value of the early 401 on ``/mcp``.

    The SDK's own refusal shape; with OAuth on it also points at the
    protected-resource metadata and names the scope, which Claude and
    ChatGPT need to start sign-in.
    """
    description = "Authentication required"
    parts = ['error="invalid_token"', f'error_description="{description}"']
    if oauth_enabled():
        parts.append(f'resource_metadata="{resource_metadata_url()}"')
        parts.append(f'scope="{MCP_SCOPE}"')
    return "Bearer " + ", ".join(parts)


def _is_bearer(value: bytes) -> bool:
    """``Bearer <non-empty token>`` (scheme case-insensitive, as the SDK)."""
    scheme, _, token = value.partition(b" ")
    return scheme.lower() == b"bearer" and bool(token.strip())


async def _unauthorized(
    challenge: str, scope: Scope, receive: Receive, send: Send
) -> None:
    await JSONResponse(
        {"error": "invalid_token", "error_description": "Authentication required"},
        status_code=401,
        headers={"WWW-Authenticate": challenge},
    )(scope, receive, send)


async def _throttled(scope: Scope, receive: Receive, send: Send) -> None:
    await JSONResponse(
        {"error": "temporarily_unavailable"},
        status_code=429,
        headers={"Retry-After": str(_MINUTE)},
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
    mcp_starlette = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        max_request_body_size=MCP_REQUEST_MAX_BYTES,
        transport_security=_transport_security(),
        host="0.0.0.0",
    )
    oauth_starlette = (
        build_oauth_app(DjangoOAuthProvider()) if oauth_enabled() else None
    )
    return GatewayDispatcher(mcp_starlette, oauth_starlette)


application = _build_application()
