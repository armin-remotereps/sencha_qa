"""The SDK's authorization server provider, backed by the OAuth services.

``DjangoOAuthProvider`` implements the MCP SDK's
``OAuthAuthorizationServerProvider``. Each method runs one sync use case of
``mcp_gateway.services.oauth_*`` in a worker thread
(``database_sync_to_async``, ``thread_sensitive=False``: every service opens
and closes its own transaction) and maps the plain types of
``mcp_gateway.oauth_types`` and the domain errors of
``mcp_gateway.exceptions`` to the SDK's pydantic models and error classes.
The SDK handlers do the protocol validation (PKCE, exact redirect match,
client authentication, refresh scope subset, RFC 7591 parsing).

``OAuthRegistrationUnavailable`` (the live client cap is reached) is not
mapped: RFC 7591 has no error code for "closed for now" and the SDK's
``RegistrationHandler`` answers every ``RegistrationError`` with 400, which
a client reads as "your metadata is wrong". It propagates to the register
route of ``mcp_gateway.oauth_app``, which answers 503
``temporarily_unavailable``.

Bearer verification on ``/mcp`` does not go through this provider (the
gateway's ``BearerVerifier`` does it); ``load_access_token`` serves only the
revocation endpoint.
"""

from __future__ import annotations

from typing import Final

from channels.db import database_sync_to_async
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationErrorCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    RegistrationErrorCode,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from mcp_gateway.exceptions import (
    OAuthAuthorizeRefused,
    OAuthGrantRefused,
    OAuthRegistrationRefused,
)
from mcp_gateway.oauth_types import AuthorizationInput, TokenPair
from mcp_gateway.services.authenticate_bearer import (
    McpAccessToken,
    load_oauth_access_token,
)
from mcp_gateway.services.oauth_clients import load_client
from mcp_gateway.services.oauth_exchange_code import exchange_code, load_code
from mcp_gateway.services.oauth_refresh import load_refresh, refresh
from mcp_gateway.services.oauth_register_client import register_client
from mcp_gateway.services.oauth_revoke import revoke_by_token
from mcp_gateway.services.oauth_start_authorization import start_authorization

_REGISTRATION_CODES: Final[dict[str, RegistrationErrorCode]] = {
    "invalid_redirect_uri": "invalid_redirect_uri",
    "invalid_client_metadata": "invalid_client_metadata",
}
_AUTHORIZE_CODES: Final[dict[str, AuthorizationErrorCode]] = {
    "invalid_request": "invalid_request",
    "invalid_scope": "invalid_scope",
    "invalid_target": "invalid_target",
    "unauthorized_client": "unauthorized_client",
    "access_denied": "access_denied",
    "temporarily_unavailable": "temporarily_unavailable",
}


def _oauth_token(pair: TokenPair) -> OAuthToken:
    return OAuthToken(
        access_token=pair.access_token,
        token_type="Bearer",
        expires_in=pair.expires_in,
        scope=pair.scope,
        refresh_token=pair.refresh_token,
    )


class DjangoOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, McpAccessToken]
):
    """The authorization server state lives in Postgres (``mcp_gateway``
    OAuth models); this class holds none, so any uvicorn worker serves any
    step of a flow."""

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        stored = await database_sync_to_async(load_client, thread_sensitive=False)(
            client_id
        )
        if stored is None:
            return None
        return OAuthClientInformationFull.model_validate(
            {**stored.metadata, "client_secret": stored.client_secret}
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        metadata = client_info.model_dump(mode="json", exclude={"client_secret"})
        try:
            await database_sync_to_async(register_client, thread_sensitive=False)(
                metadata, client_info.client_secret
            )
        except OAuthRegistrationRefused as exc:
            raise RegistrationError(
                error=_REGISTRATION_CODES.get(exc.code, "invalid_client_metadata"),
                error_description=exc.message,
            ) from exc

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        request = AuthorizationInput(
            client_id=client.client_id,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            code_challenge=params.code_challenge,
            state=params.state,
            scopes=params.scopes,
            resource=params.resource,
        )
        try:
            return await database_sync_to_async(
                start_authorization, thread_sensitive=False
            )(request)
        except OAuthAuthorizeRefused as exc:
            raise AuthorizeError(
                error=_AUTHORIZE_CODES.get(exc.code, "invalid_request"),
                error_description=exc.message,
            ) from exc

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        issued = await database_sync_to_async(load_code, thread_sensitive=False)(
            client.client_id, authorization_code
        )
        if issued is None:
            return None
        return AuthorizationCode(
            code=issued.code,
            scopes=issued.scopes,
            expires_at=issued.expires_at,
            client_id=issued.client_id,
            code_challenge=issued.code_challenge,
            redirect_uri=AnyUrl(issued.redirect_uri),
            redirect_uri_provided_explicitly=issued.redirect_uri_provided_explicitly,
            resource=issued.resource,
            subject=issued.subject,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        try:
            pair = await database_sync_to_async(exchange_code, thread_sensitive=False)(
                client.client_id, authorization_code.code
            )
        except OAuthGrantRefused as exc:
            raise TokenError(
                error="invalid_grant", error_description=exc.message
            ) from exc
        return _oauth_token(pair)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        stored = await database_sync_to_async(load_refresh, thread_sensitive=False)(
            client.client_id, refresh_token
        )
        if stored is None:
            return None
        return RefreshToken(
            token=stored.token,
            client_id=stored.client_id,
            scopes=stored.scopes,
            expires_at=stored.expires_at,
            resource=stored.resource,
            subject=stored.subject,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        try:
            pair = await database_sync_to_async(refresh, thread_sensitive=False)(
                client.client_id, refresh_token.token, scopes
            )
        except OAuthGrantRefused as exc:
            raise TokenError(
                error="invalid_grant", error_description=exc.message
            ) from exc
        return _oauth_token(pair)

    async def load_access_token(self, token: str) -> McpAccessToken | None:
        return await database_sync_to_async(
            load_oauth_access_token, thread_sensitive=False
        )(token)

    async def revoke_token(self, token: McpAccessToken | RefreshToken) -> None:
        # Revokes the whole grant (access and refresh), as the SDK advises.
        await database_sync_to_async(revoke_by_token, thread_sensitive=False)(
            token.client_id, token.token
        )
