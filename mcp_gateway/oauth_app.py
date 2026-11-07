"""The OAuth 2.1 authorization server endpoints of the MCP gateway.

``build_oauth_app(provider)`` returns a Starlette app with explicit routes
built from the MCP SDK's handler classes (the same handlers, CORS and body
limit wrapping as the SDK's ``create_auth_routes``) and our own metadata
document. ``create_auth_routes`` is not used: its metadata cannot advertise
the ``none`` token endpoint auth method (public DCR clients: Claude,
ChatGPT), and ``MCPServer`` refuses an ``auth_server_provider`` together
with our own token verifier.

Routes, all at the site root (the issuer is ``SITE_URL`` without a path):

- ``/.well-known/oauth-authorization-server``: RFC 8414 metadata.
- ``/.well-known/oauth-protected-resource``: the RFC 9728 document of the
  ``/mcp`` resource, served at the root as well (the clients' fallback
  probe); the ``/mcp`` path-inserted one comes from the MCP Starlette app.
- ``/authorize``: validates the request and 302s to the Django consent page
  (``DjangoOAuthProvider.authorize``); no CORS (a browser navigation).
- ``/token``, ``/register`` and ``/revoke``.

``auto_tester.mcp_asgi`` routes exactly ``OAUTH_PATHS`` here (only while
``MCP_OAUTH_ENABLED``), after its per-IP throttles and body cap. The app has
no lifespan: it holds no state (the provider's is in Postgres).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final

from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.metadata import (
    MetadataHandler,
    ProtectedResourceMetadataHandler,
)
from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.handlers.revoke import RevocationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.routes import (
    AUTHORIZATION_PATH,
    REGISTRATION_PATH,
    REVOCATION_PATH,
    TOKEN_PATH,
    validate_issuer_url,
)
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.server.transport_security import RequestBodyLimitMiddleware
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata
from mcp.shared.inbound import MCP_PROTOCOL_VERSION_HEADER
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, request_response
from starlette.types import ASGIApp

from mcp_gateway.exceptions import OAuthRegistrationUnavailable
from mcp_gateway.limits import MCP_OAUTH_REQUEST_MAX_BYTES
from mcp_gateway.oauth_policy import (
    MCP_SCOPE,
    PROTECTED_RESOURCE_PATH,
    canonical_resource,
    issuer,
    resource_matches,
)
from mcp_gateway.oauth_provider import DjangoOAuthProvider

AUTHORIZATION_SERVER_METADATA_PATH: Final = "/.well-known/oauth-authorization-server"
# Every path this app serves; the ASGI host dispatches exactly these here.
OAUTH_PATHS: Final = frozenset(
    {
        AUTHORIZATION_SERVER_METADATA_PATH,
        PROTECTED_RESOURCE_PATH,
        AUTHORIZATION_PATH,
        TOKEN_PATH,
        REGISTRATION_PATH,
        REVOCATION_PATH,
    }
)
_TOKEN_AUTH_METHODS: Final = ["none", "client_secret_post", "client_secret_basic"]
_REGISTRATION_CLOSED_RETRY_SECONDS: Final = 3600

_Endpoint = Callable[[Request], Awaitable[Response]]


def _cors(app: ASGIApp, allow_methods: list[str]) -> ASGIApp:
    """The SDK's CORS wrapping (outermost, so a 413 carries CORS headers)."""
    return CORSMiddleware(
        app=app,
        allow_origins="*",
        allow_methods=allow_methods,
        allow_headers=[MCP_PROTOCOL_VERSION_HEADER],
    )


def _body_limited(endpoint: _Endpoint) -> ASGIApp:
    return RequestBodyLimitMiddleware(
        request_response(endpoint), MCP_OAUTH_REQUEST_MAX_BYTES
    )


def _authorization_server_metadata(issuer_url: str) -> OAuthMetadata:
    # Built from strings with ``model_validate``: the model keeps a path-less
    # issuer as typed (no trailing slash), so ``issuer`` equals the
    # protected-resource document's ``authorization_servers`` entry exactly.
    return OAuthMetadata.model_validate(
        {
            "issuer": issuer_url,
            "authorization_endpoint": issuer_url + AUTHORIZATION_PATH,
            "token_endpoint": issuer_url + TOKEN_PATH,
            "registration_endpoint": issuer_url + REGISTRATION_PATH,
            "revocation_endpoint": issuer_url + REVOCATION_PATH,
            "scopes_supported": [MCP_SCOPE],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": _TOKEN_AUTH_METHODS,
            "revocation_endpoint_auth_methods_supported": _TOKEN_AUTH_METHODS,
        }
    )


def _protected_resource_metadata(issuer_url: str) -> ProtectedResourceMetadata:
    """The same document the SDK serves at ``.../oauth-protected-resource/mcp``."""
    return ProtectedResourceMetadata.model_validate(
        {
            "resource": canonical_resource(),
            "authorization_servers": [issuer_url],
            "scopes_supported": [MCP_SCOPE],
        }
    )


def _registration_endpoint(handler: RegistrationHandler) -> _Endpoint:
    """The SDK registration handler, plus a 503 while registration is closed
    (the live client cap; the provider lets that error through)."""

    async def register(request: Request) -> Response:
        try:
            return await handler.handle(request)
        except OAuthRegistrationUnavailable as exc:
            return JSONResponse(
                {"error": "temporarily_unavailable", "error_description": exc.message},
                status_code=503,
                headers={"Retry-After": str(_REGISTRATION_CLOSED_RETRY_SECONDS)},
            )

    return register


def _token_endpoint(handler: TokenHandler) -> _Endpoint:
    """The SDK token handler, plus the RFC 8707 check it does not make: a
    ``resource`` sent to /token must name this server (the code or refresh
    token was bound to it at /authorize). The form is parsed once; Starlette
    caches it on the request the handler reads."""

    async def token(request: Request) -> Response:
        form = await request.form()
        resources = form.getlist("resource")
        if any(
            not isinstance(value, str) or not resource_matches(value)
            for value in resources
        ):
            return JSONResponse(
                {
                    "error": "invalid_target",
                    "error_description": "The resource is not this server.",
                },
                status_code=400,
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )
        # The SDK's ``TokenHandler.handle`` is unannotated; it returns a Response.
        response: Response = await handler.handle(request)
        return response

    return token


def build_oauth_app(provider: DjangoOAuthProvider) -> Starlette:
    """The OAuth endpoints (call only with ``MCP_OAUTH_ENABLED``)."""
    issuer_url = issuer()
    # https, or http on loopback; no query or fragment (settings check the
    # same at startup).
    validate_issuer_url(AnyHttpUrl(issuer_url))
    client_authenticator = ClientAuthenticator(provider)
    metadata_handler = MetadataHandler(_authorization_server_metadata(issuer_url))
    resource_handler = ProtectedResourceMetadataHandler(
        _protected_resource_metadata(issuer_url)
    )
    registration_handler = RegistrationHandler(
        provider,
        options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[MCP_SCOPE], default_scopes=[MCP_SCOPE]
        ),
    )
    routes = [
        Route(
            AUTHORIZATION_SERVER_METADATA_PATH,
            endpoint=_cors(
                request_response(metadata_handler.handle), ["GET", "OPTIONS"]
            ),
            methods=["GET", "OPTIONS"],
        ),
        Route(
            PROTECTED_RESOURCE_PATH,
            endpoint=_cors(
                request_response(resource_handler.handle), ["GET", "OPTIONS"]
            ),
            methods=["GET", "OPTIONS"],
        ),
        Route(
            AUTHORIZATION_PATH,
            # No CORS: clients navigate the browser here.
            endpoint=_body_limited(AuthorizationHandler(provider).handle),
            methods=["GET", "POST"],
        ),
        Route(
            TOKEN_PATH,
            endpoint=_cors(
                _body_limited(
                    _token_endpoint(TokenHandler(provider, client_authenticator))
                ),
                ["POST", "OPTIONS"],
            ),
            methods=["POST", "OPTIONS"],
        ),
        Route(
            REGISTRATION_PATH,
            endpoint=_cors(
                _body_limited(_registration_endpoint(registration_handler)),
                ["POST", "OPTIONS"],
            ),
            methods=["POST", "OPTIONS"],
        ),
        Route(
            REVOCATION_PATH,
            endpoint=_cors(
                _body_limited(RevocationHandler(provider, client_authenticator).handle),
                ["POST", "OPTIONS"],
            ),
            methods=["POST", "OPTIONS"],
        ),
    ]
    return Starlette(routes=routes)
