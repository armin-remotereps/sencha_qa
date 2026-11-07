# feat: OAuth 2.1 sign-in for the MCP gateway (ChatGPT and claude.ai connectors)

Status: Done
Created: 2026-11-07
Request: The remote MCP gateway at `https://<host>/mcp` accepts only the project's Connection key as a fixed Bearer token. ChatGPT connectors (developer mode / Apps) and claude.ai custom connectors expect a standard MCP OAuth 2.1 flow instead: they discover the authorization server, register themselves, send the person through a browser sign-in and consent, and then use short-lived access tokens with refresh. Add that flow: the Django site acts as the authorization server, a signed-in member picks one project they can write to on a consent page, and the token is bound to that user and project. The gateway accepts either an OAuth access token or the Connection key, so Codex CLI, the OpenAI Responses API and Agents SDK, and Claude Code keep working unchanged. Users can see and revoke their connections. Tokens are stored hashed, expire, and rotate. The auth endpoints are throttled and guarded against open redirects, CSRF and registration abuse.

## Context

- **Gateway** (commit f7d4a70, `docs/plan/feat-controller-mcp.md`, `docs/changed/2026-11-07-controller-mcp.md`):
  - `auto_tester/mcp_asgi.py`: the uvicorn app (compose service `mcp`, `127.0.0.1:8001`, 2 workers, `--proxy-headers`). `RequestBodyCap` answers **every** HTTP request without a `Bearer` header with 401 before reading the body. Its 401 has no `resource_metadata`. It caps the body at 10 MiB and publishes the client IP in `MCP_CLIENT_IP`. `_transport_security()` builds the Host/Origin check from `ALLOWED_HOSTS`. It builds `server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, ...)`.
  - `mcp_gateway/mcp_app.py` `build_mcp_server()`: `MCPServer(token_verifier=ProjectKeyVerifier(), auth=AuthSettings(issuer_url=SITE_URL or http://localhost, resource_server_url=None, validate_token_resource=False))`. With `resource_server_url=None` no protected-resource metadata is served and the 401 from `RequireAuthMiddleware` carries no `resource_metadata`. `_run()` gets the project with `project_id_of(get_access_token())`.
  - `mcp_gateway/services/authenticate_project_key.py`: `ProjectKeyVerifier.verify_token`. The token must be at most 64 characters. It is looked up with `projects.services.get_project_by_api_key`, re-checked with `hmac.compare_digest`, and failures are counted per IP. It returns `AccessToken(client_id=str(project_id), scopes=[])`. `project_id_of` parses `client_id`.
  - `mcp_gateway/services/call_guard.py` `guarded_call` → `_resolve_call(project_id)`: loads the non-archived project and always uses `PersonStepActor.tester()` ("a tester"). It throttles per project and kind and logs one line per call.
  - `mcp_gateway/limits.py`: operational constants. `mcp_gateway/tasks.py`: hourly `sweep_mcp_staged_shots` (`notify`). The app has no views, urls, forms or templates.
  - `nginx/server.conf`: `location /mcp` → `mcp:8001`, with `X-Forwarded-For` overwritten. Everything else goes to `api:8000` (Daphne/Django). Nothing routes `/.well-known/*`, `/authorize`, `/token`, `/register` or `/revoke`. None of these paths exist in Django (`auto_tester/urls.py`, `accounts/urls.py`, `dashboard/urls.py`).
- **Access rules**:
  - `projects/project_access_services.get_project_access`: one query; a member's role, or implicit superuser access.
  - `projects/project_access.has_superuser_project_control` and `ProjectAccess.can_write`.
  - `projects/models.WRITABLE_ROLES` (only `ProjectRole.MEMBER`) and `ProjectMembership(project, user, role)`.
  - `Project.api_key` (unique, 64 chars, `secrets.token_urlsafe(32)`). `projects/services.regenerate_api_key` saves a new one.
- **Secrets helper**: `projects/secrets.encrypt_secret` / `decrypt_secret` (Fernet, `FIELD_ENCRYPTION_KEY`, already required).
- **Actor**: `projects/person_step_actor.PersonStepActor.tester()` → "a tester". The original plan deliberately shows nothing in the app or TestRail that says an AI performed a case.
- **Django**: `XFrameOptionsMiddleware` is on, so the consent page cannot be framed. `CsrfViewMiddleware` is on. `LOGIN_URL=/accounts/login/`. `SITE_URL` setting exists (default ""). Layouts are `templates/layouts/{app,auth}.html`, and the nav is in `templates/partials/nav_links.html`.
- **Versions**: `mcp~=2.3.0` (2.3.0 read from the wheel; the dev venv has no `mcp` installed), `uvicorn~=0.54.0`, `Django~=5.2.11` (5.2.17 installed). No OAuth library is installed.

### What the MCP SDK 2.3.0 gives us (read from source)

- `mcp.server.auth.provider.OAuthAuthorizationServerProvider` protocol has these methods: `get_client`, `register_client`, `authorize` (returns the URL to send the browser to; this is where we hand off to the Django consent page), `load_authorization_code`, `exchange_authorization_code`, `load_refresh_token`, `exchange_refresh_token`, `load_access_token` and `revoke_token`. `AccessToken`, `AuthorizationCode` and `RefreshToken` are pydantic models. The SDK says subclasses may add fields.
- Handlers (public classes, usable one by one) in `mcp.server.auth.handlers.*`:
  - `AuthorizationHandler`: checks `response_type=code`, requires `code_challenge`, allows only `S256`, matches the redirect URI exactly against the client's registered list, validates scope and passes `resource` through.
  - `TokenHandler`: checks code expiry and `client_id`, that the redirect URI is unchanged, and the PKCE S256 verifier. It handles refresh with expiry and scope subset. Client authentication failure → 401 `invalid_client`.
  - `RegistrationHandler` (RFC 7591): mints a uuid `client_id`, and a secret unless `token_endpoint_auth_method=none`. It refuses `private_key_jwt` and requires `authorization_code` and `code`. It then calls `register_client`, which may raise `RegistrationError`.
  - `RevocationHandler` (RFC 7009) and `MetadataHandler`.
  - `ClientAuthenticator` compares the **plaintext** `client_secret` returned by `get_client` (so stored secrets must be recoverable: encrypt, do not hash).
- `create_auth_routes()` mounts all of these at **fixed root paths** `/authorize`, `/token`, `/register`, `/revoke` and `/.well-known/oauth-authorization-server`. Its metadata is fixed: `token_endpoint_auth_methods_supported=["client_secret_post","client_secret_basic"]` (no `none`), and there is no way to add fields. We therefore build the routes ourselves from the handler classes with our own metadata document, instead of passing `auth_server_provider` to `MCPServer`.
- With `AuthSettings.resource_server_url` set, `streamable_http_app` adds three things:
  - the RFC 9728 route `/.well-known/oauth-protected-resource/mcp`;
  - `resource_metadata="…"` in the `WWW-Authenticate` of `RequireAuthMiddleware`'s 401;
  - with `validate_token_resource=True`, a check in `BearerAuthBackend` that rejects any `AccessToken.resource` that is not `resource_server_url`.

  `required_scopes` is enforced by `RequireAuthMiddleware` (403 `insufficient_scope`) and published as `scopes_supported`.
- There is no Client ID Metadata Document (CIMD) handler. Only the metadata field exists.

### What the clients require (researched 2026-11-07)

- **MCP spec 2025-11-25** ([authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)):
  - The server MUST serve RFC 9728 protected-resource metadata. The 401 should point at it with `WWW-Authenticate: Bearer resource_metadata=…`, and SHOULD carry `scope`. Clients probe `/.well-known/oauth-protected-resource/<path>` and then the root path.
  - For an issuer without a path, AS metadata is at `/.well-known/oauth-authorization-server` (then `openid-configuration`).
  - `code_challenge_methods_supported` must include `S256`, or the client refuses.
  - Clients MUST send `resource` (RFC 8707) on both authorize and token requests, and the server MUST check that the token was issued for it.
  - Public clients MUST get rotating refresh tokens.
  - Redirect URIs must match exactly and be https or localhost. The consent page MUST show the redirect host, and should warn when the only redirect is loopback.
  - Registration preference: pre-registered, then CIMD, then DCR.
- **ChatGPT** ([Apps SDK › Authentication](https://developers.openai.com/apps-sdk/build/auth)):
  - Client identity: CIMD (preferred when advertised), DCR (one registration per connection, then reused; the client must stay valid or the user sees `invalid_client`) or a predefined client.
  - PKCE S256 is required. It sends `resource=<canonical MCP URL>` on authorize and token requests. Token endpoint auth: `none` (public) or `private_key_jwt` (CIMD only).
  - Redirect URI is `https://chatgpt.com/connector_platform_oauth_redirect` when the server is RFC 9207 issuer-identification compliant, otherwise `https://chatgpt.com/connector/oauth/{callback_id}`.
  - The `issuer` must exactly equal the entry in `authorization_servers`. Every advertised scope must be enabled for the client.
  - On a 401 it expects `WWW-Authenticate: Bearer resource_metadata=…` plus `scope`.
- **Claude** ([Authentication for connectors](https://claude.com/docs/connectors/building/authentication), [support article](https://support.claude.com/en/articles/11503834-building-custom-connectors-via-remote-mcp-servers)):
  - A 401 with `resource_metadata` is required to start sign-in. Claude uses only the first `authorization_servers` entry. `resource` must equal the URL the user typed.
  - Claude uses CIMD only when the AS advertises `client_id_metadata_document_supported` **and** `none`, otherwise DCR. **DCR registers Claude as a public client, and registers a new client on every fresh connection.**
  - PKCE S256 on every request.
  - Hosted callback is `https://claude.ai/api/mcp/auth_callback` (allowlisting claude.com is advised). Claude Code uses a loopback `http://localhost:<port>/callback`.
  - Scopes: it requests the 401's `scope`, else `scopes_supported`. It appends `offline_access` only when the AS lists it.
  - Refresh: reactive on 401 and proactive 5 minutes before expiry. An invalid refresh must answer `invalid_grant`. Rotation is required for public clients.
  - `/token` is form-urlencoded and `/register` is JSON. Timeouts: 10 s for discovery, registration and token; 30 s for refresh. A 401 `invalid_client` from `/token` makes it register again.
  - Egress range is `160.79.104.0/21`, so many users share IPs.
- **django-oauth-toolkit** (3.4 docs, [DCR](https://django-oauth-toolkit.readthedocs.io/en/latest/views/dynamic_client_registration.html)): now has RFC 7591/7592 DCR, off by default. I found no RFC 8707 `resource` binding and no RFC 9728 metadata. A third-party package reports that `token_endpoint_auth_method` is not modelled (registrations downgraded to confidential).

## Goals

- ChatGPT connectors and claude.ai custom connectors can add `https://<host>/mcp` with **OAuth** and no manual client setup (DCR).
- A signed-in Django user approves one project they can write to. The token acts only on that project and only while that user still has write access.
- Codex CLI, the OpenAI APIs, Claude Code and Claude's "Request header" connectors keep using the Connection key with no change.
- Tokens are opaque, stored as SHA-256 hashes, short-lived (access) and rotating (refresh), and revocable by the user. Regenerating the Connection key also cuts every OAuth connection of the project.
- One switch turns the whole OAuth surface off.

## Non-goals

- CIMD (Client ID Metadata Documents): it needs SSRF-safe fetching and caching, and the SDK has no handler. Both clients fall back to DCR when it is not advertised. It can be a follow-up.
- `private_key_jwt`, OpenID Connect (`openid`/`email`/userinfo, ChatGPT workspace domain restriction), Enterprise Managed Auth / ID-JAG, RFC 9207 `iss` in responses, and pre-registered static clients (Anthropic-held credentials or a predefined ChatGPT client).
- Per-tool scopes or a read-only grant. There is one scope with the same tool set as the Connection key.
- Changing how MCP actions are attributed in the app or TestRail. They stay "a tester" (see Q3).
- Any change to the twelve tools, their throttles, or the Test Runner protocol. This is not a controller protocol change: `CLIENT_VERSION`, capabilities and runners are untouched.
- Tests (project rule).

## Design

### Where the authorization server lives

**The uvicorn `mcp` service serves the OAuth protocol endpoints. Django serves the human part (sign-in, consent, the connections page).** The issuer is the site root (`SITE_URL`, e.g. `https://sench.remotereps.com`).

- Protocol endpoints use the SDK handlers in the `mcp` process. They are pure HTTP+JSON/form and need no Django session, and keeping them there reuses the SDK's tested validation (PKCE, redirect matching, client auth, refresh scope checks, RFC 7591 parsing).
- `/authorize` is a validated hand-off: the provider's `authorize()` stores a pending request and returns the URL of a Django consent page. Django already owns login, sessions, CSRF, templates and membership, so the consent page and the revoke page are ordinary Django views.
- Why the issuer is the root and not `/mcp` or a sub-path:
  - the SDK handlers register at fixed root paths;
  - an issuer with a path needs path-inserted well-known URLs that the SDK does not serve;
  - Claude and ChatGPT both try `/.well-known/oauth-authorization-server` first for a path-less issuer.

  Cost: four short root paths on the main site go to the `mcp` service. Django has no route on them today.

nginx (`nginx/server.conf`), new exact-match locations proxied to `mcp:8001` with the same forwarded-header block as `/mcp` (`X-Forwarded-For` overwritten), small body cap (`client_max_body_size 64k`), and access-log formats unchanged:

| Path | Method | Served by |
|---|---|---|
| `= /.well-known/oauth-protected-resource` and `= /.well-known/oauth-protected-resource/mcp` | GET, OPTIONS | `mcp`. The `/mcp` one comes from the SDK. The root one is ours, with the same document; it is the ChatGPT and Claude fallback probe. |
| `= /.well-known/oauth-authorization-server` | GET, OPTIONS | `mcp` (our metadata) |
| `= /authorize` | GET, POST | `mcp` (SDK `AuthorizationHandler` → 302 to the Django consent page) |
| `= /token` | POST, OPTIONS | `mcp` (SDK `TokenHandler`) |
| `= /register` | POST, OPTIONS | `mcp` (SDK `RegistrationHandler`) |
| `= /revoke` | POST, OPTIONS | `mcp` (SDK `RevocationHandler`) |
| `/connected-apps/…` | | Django (`api`), via the existing `location /` |

`/.well-known/openid-configuration` stays a Django 404, which tells clients that OIDC is not supported.

### Data model (`mcp_gateway/models.py`, migration `mcp_gateway/0002_oauth`, additive)

All token-like values are random (`secrets.token_urlsafe(32)`, 256 bits). Only `sha256` hex digests are stored, as unique indexed columns. A fast hash is right for high-entropy secrets, and lookup is one indexed equality. Access and refresh tokens carry a readable prefix (`pha_` / `phr_`) so the verifier knows which table to try. Codes and consent handles are unprefixed.

- **`McpOAuthClient`**: one dynamically registered client.
  - Columns: `client_id` (SDK uuid, unique), `client_name` (≤ 100 chars, untrusted, control characters stripped), `redirect_uris` (JSON list, each validated by policy), `token_endpoint_auth_method` (`none` / `client_secret_post` / `client_secret_basic`), `client_secret_encrypted` (Fernet via `projects.secrets`, blank for public clients; the SDK compares plaintext), `client_secret_expires_at` (0), `scope` ("mcp"), `metadata` (JSON dump of the validated RFC 7591 request minus the secret, used to rebuild `OAuthClientInformationFull` in `get_client`), `created_at`, `last_used_at` (null until the first code is issued).
  - Index on `created_at` for the sweep.
- **`McpAuthorizationRequest`**: a pending consent, created by `/authorize`, single use.
  - Columns: `handle_hash` (unique), `client` FK, `redirect_uri`, `redirect_uri_provided_explicitly`, `code_challenge`, `state` (≤ 512 chars; longer is refused), `scopes` (JSON), `resource`, `created_at`, `expires_at` (+10 min), `consumed_at`.
- **`McpAuthorizationCode`**: issued on approval, single use.
  - Columns: `code_hash` (unique), `client` FK, `user` FK, `project` FK, `code_challenge`, `redirect_uri`, `redirect_uri_provided_explicitly`, `resource`, `scopes`, `expires_at` (+60 s), `used_at`, `grant` FK (null until exchanged; used for the replay revoke).
- **`McpGrant`**: one connection, the unit the user sees and revokes.
  - Columns: `client` FK, `user` FK, `project` FK, `scopes`, `resource`, `api_key_fingerprint` (sha256 of `Project.api_key` at consent), `created_at`, `last_used_at`, `revoked_at`, `revoked_reason` (`user` / `client` / `refresh_reuse` / `code_reuse` / `membership`).
  - Index on (`user`, `revoked_at`) for the page and (`project`) for project-wide checks.
- **`McpOAuthToken`**: access and refresh tokens.
  - Columns: `token_hash` (unique), `kind` (`access` / `refresh`), `grant` FK, `expires_at`, `created_at`, `rotated_at` (refresh only, set when exchanged).
  - Access 1 h. Refresh 30 days from issue: rotating, so an active connection lives indefinitely and an idle one dies after 30 days.

Lifetimes and caps are constants in `mcp_gateway/limits.py`, following the existing "constants, not settings" rule: `MCP_OAUTH_ACCESS_TTL_SECONDS=3600`, `MCP_OAUTH_REFRESH_TTL_SECONDS=30*86400`, `MCP_OAUTH_CODE_TTL_SECONDS=60`, `MCP_OAUTH_CONSENT_TTL_SECONDS=600`, `MCP_OAUTH_REFRESH_REUSE_GRACE_SECONDS=30`, plus the throttles below.

No change to `projects` models. There is no hook in `projects.regenerate_api_key`: the key fingerprint is compared on read, which keeps the import direction `mcp_gateway → projects`.

### Policy (pure, no ORM): `mcp_gateway/oauth_policy.py`

- `MCP_SCOPE = "mcp"`: the single scope, required on every token, Connection-key tokens included.
- `canonical_resource()` = `SITE_URL.rstrip("/") + "/mcp"`, and `issuer()` = `SITE_URL.rstrip("/")`, both from settings. `resource_matches(value)` uses the SDK's comparison: scheme and host case-insensitive, default port normalised, one trailing slash ignored.
- `redirect_uri_allowed(uri)`:
  - `https` with no fragment, and the host equal to an entry of `MCP_OAUTH_REDIRECT_HOSTS` (exact host, no wildcard);
  - or `http` with a loopback host (`localhost`, `127.0.0.1`, `[::1]`) when that host is listed. Loopback is matched by exact URI per registration, because DCR clients register the exact port.
- `client_display_name(redirect_uri)`: the trusted name from the redirect host (`chatgpt.com` → "ChatGPT", `claude.ai` / `claude.com` → "Claude", loopback → "An app on your computer", otherwise the host). The consent page leads with this. The self-declared `client_name` is shown only as secondary text.
- `mint_token(prefix)` and `hash_token(value)`: stateless helpers, in `mcp_gateway/oauth_tokens.py`.

### Services (`mcp_gateway/services/`, one use case per module, sync, transactions inside)

| Module | Use case |
|---|---|
| `oauth_register_client.py` | `register_client(info)`. Applies the redirect allowlist, name sanitising, auth method in {none, client_secret_post, client_secret_basic} and scope = {"mcp"}. Encrypts the secret and saves. Refusals raise `OAuthRegistrationRefused(code, text)`. A global cap of live clients (`MCP_OAUTH_MAX_CLIENTS = 5000`; over the cap → refused with a warning log). |
| `oauth_clients.py` | `load_client(client_id) -> OAuthClientInformationFull or None`. Decrypts the secret. Unknown or swept clients → None, which makes `/token` answer 401 `invalid_client`, and Claude re-registers. |
| `oauth_start_authorization.py` | `start_authorization(client, params) -> consent_url`. `resource` must be absent or match `canonical_resource()`, else `invalid_target`; absent is stored as canonical. Stores the request and returns `SITE_URL + reverse("mcp_gateway:consent", handle)`. |
| `oauth_consent.py` | `load_consent(handle, user) -> ConsentView`: client display name, secondary name, redirect host, loopback flag, the user's writable projects, expired/consumed state. Then `approve(handle, user, project_id) -> redirect_url` and `deny(handle, user) -> redirect_url`. Both lock the request row and refuse it if it is consumed or expired. `approve` re-checks write access to the chosen project (rule below), mints the code, and returns `redirect_uri?code=…&state=…` built with the SDK's `construct_redirect_uri`. `deny` returns `…?error=access_denied&state=…`. Both consume the request. |
| `oauth_exchange_code.py` | `load_code(client_id, code)` and `exchange_code(client_id, code) -> OAuthToken`. Locks the code row. An already used code → revoke the grant it produced (`code_reuse`) and raise `invalid_grant`. Otherwise it re-checks write access, creates the `McpGrant` with the key fingerprint, then issues access + refresh. Response: `token_type="Bearer"`, `expires_in=3600`, `scope="mcp"`. |
| `oauth_refresh.py` | `load_refresh(client_id, token)` and `refresh(client_id, token) -> OAuthToken`. Locks the token row. An already rotated token inside `MCP_OAUTH_REFRESH_REUSE_GRACE_SECONDS` → `invalid_grant` with no revoke (Claude's proactive and reactive refreshes can race). Outside the grace period → revoke the grant (`refresh_reuse`) and `invalid_grant`. A live grant that fails the access re-check → revoke (`membership`) and `invalid_grant`. Otherwise mark the token rotated and issue a new pair. |
| `authenticate_bearer.py` (replaces `authenticate_project_key.py`; the key logic moves in unchanged) | See the verifier below. |
| `oauth_revoke.py` | `revoke_by_token(client_id, token)` for RFC 7009 (revokes the whole grant, as the SDK recommends; unknown → no-op). `revoke_grant_for_user(grant_id, user)` for the page (only the owner's grant, else `GrantNotFound`). |
| `oauth_connections.py` | `list_connections(user) -> list[ConnectionRow]`: one query (`select_related("client", "project")`, newest first, revoked ones from the last 30 days included). The status is computed on read: active, revoked, or not valid any more (no write access / key regenerated / project archived / refresh expired). |
| `oauth_access.py` | `writable_project_ids(user)`, and the access predicate shared by consent, exchange, refresh and the verifier. |
| `sweep.py` (extend) | Delete pending requests and codes past expiry + 1 day, tokens past expiry + 1 day, grants revoked or idle (no live refresh token) for more than 30 days, clients with no grant that are more than 24 h old, and clients whose last grant ended more than 90 days ago. Each step is a bounded set-based delete. |

**Access rule** (draft, see Q1/Q2). The user is active, and has an active **writable** `ProjectMembership` (`role in WRITABLE_ROLES`) on a non-archived project. The grant is not revoked, and `sha256(project.api_key) == grant.api_key_fingerprint`. Superuser implicit access is **not** used for OAuth grants, and read-only members cannot approve. The rule is checked at consent, at code exchange, at every refresh and on **every MCP request**, so losing write access, archiving the project or regenerating the key takes effect at the next call.

Domain errors: add `OAuthRegistrationRefused`, `OAuthAuthorizeRefused`, `OAuthGrantRefused`, `ConsentUnavailable` and `GrantNotFound` to `mcp_gateway/exceptions.py`. Each carries a safe message. Only the SDK adapter maps them to `RegistrationError`, `AuthorizeError` and `TokenError`.

### SDK adapter and verifier

- **`mcp_gateway/oauth_provider.py`**: `DjangoOAuthProvider` implements `OAuthAuthorizationServerProvider`. Every method is a one-line `database_sync_to_async(..., thread_sensitive=False)` call into the services above, plus error mapping. `load_access_token` is not used for request auth (the verifier is), so it delegates to the verifier.
- **`mcp_gateway/oauth_app.py`**: `build_oauth_app(provider) -> Starlette` with explicit routes built from SDK handler classes:
  - `MetadataHandler` with **our** `OAuthMetadata`: `issuer`, the authorization, token, registration and revocation endpoints, `response_types_supported=["code"]`, `grant_types_supported=["authorization_code","refresh_token"]`, `code_challenge_methods_supported=["S256"]`, `token_endpoint_auth_methods_supported=["none","client_secret_post","client_secret_basic"]`, the same list for revocation, and `scopes_supported=["mcp"]`. No `offline_access`, and no CIMD flag.
  - `AuthorizationHandler`, and `TokenHandler` + `ClientAuthenticator`.
  - `RegistrationHandler(ClientRegistrationOptions(enabled=True, valid_scopes=["mcp"], default_scopes=["mcp"]))`.
  - `RevocationHandler`.
  - The root protected-resource metadata document.

  CORS wrapping is the same as the SDK's own `create_auth_routes` (none on `/authorize`). No lifespan is needed.
- **`authenticate_bearer.BearerVerifier`** (SDK `TokenVerifier`) replaces `ProjectKeyVerifier`:
  1. Strip the token. Empty or over 64 characters → refuse with no query (unchanged).
  2. `pha_` prefix and OAuth enabled → one query: token by hash, `kind=access`, not expired, joined to grant, client, user and project, with an `Exists` writable-membership subquery. Then the fingerprint compare in Python. On a miss, fall through to step 3 so a Connection key that happens to start with `pha_` (about 1 in 16 million) still works.
  3. Otherwise → the existing Connection key lookup + `compare_digest` + per-IP failure counter, all unchanged.
  4. Return `McpAccessToken(AccessToken)` (subclass, internal only) with `project_id`, `user_id` (None for the key), `grant_id` (None for the key), `scopes=["mcp"]`, `resource=canonical_resource()`, and `expires_at` (OAuth only). `client_id` stays `str(project_id)` for the key and is the OAuth `client_id` for grants.
  5. Mark the grant used: `cache.add("mcp:grant-used:<id>", 1, 300)`, and only when the key was newly added, run one `UPDATE … SET last_used_at`. That is at most one write per grant per 5 minutes.

  `project_id_of()` reads `McpAccessToken.project_id` (isinstance check). `call_guard` and the twelve tools are otherwise unchanged. `guarded_call` gets an `auth` label (`key` / `oauth`) and `grant_id` for its log line. `_NO_PROJECT` becomes neutral ("Your access to this project is not valid any more. Reconnect.").
- **`mcp_gateway/mcp_app.py` `build_mcp_server()`**: `token_verifier=BearerVerifier()`, `auth=AuthSettings(issuer_url=issuer(), resource_server_url=canonical_resource() if oauth_enabled else None, validate_token_resource=oauth_enabled, required_scopes=["mcp"])`. Turning OAuth off therefore removes discovery and the audience check, and the key path still works.
- **`auto_tester/mcp_asgi.py`**: a small path dispatcher.
  - OAuth paths (the table above, only when enabled) → the OAuth app. Everything else → the MCP Starlette app, which keeps the lifespan.
  - `RequestBodyCap` applies the bearer pre-check **only to `/mcp`**. Today it would 401 the discovery and token endpoints.
  - The body cap is 10 MiB for `/mcp` and 64 KiB for OAuth paths.
  - Per-path throttles run before the handler (below).
  - The early 401 adds `resource_metadata="<SITE_URL>/.well-known/oauth-protected-resource/mcp"` and `scope="mcp"` when OAuth is enabled. Both Claude and ChatGPT need the pointer on a 401.

### Pages and forms (Django, `mcp_gateway/views.py`, `urls.py`, `forms.py`; included at `connected-apps/` in `auto_tester/urls.py`)

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `/connected-apps/authorize/<handle>/` (`mcp_gateway:consent`) | GET | `@login_required`. Anonymous users go to login with `next`, which keeps the handle; the handle is safe in a URL because it is single-use and lasts 10 minutes. | `ConsentForm(project: ChoiceField from the user's writable projects)` | `mcp_gateway/consent.html` (auth layout). Context: `client_display_name`, `client_declared_name` (escaped, labelled "calls itself"), `redirect_host`, `is_loopback` (warning), `projects` (id + name), `form`, `scope_text` ("Claim and perform Manual run cases, send screenshots, hand in results, read runs and the knowledge base of the chosen project"). | Unknown, expired or used handle → 400 page "This sign-in link has expired. Start again from the app" (never redirect: we cannot trust where to). No writable project → page explains this and offers only Deny. |
| same | POST | `@login_required`, CSRF, `@require_POST`. Throttle 10 / min / user (cache counter, `hit_runner_throttle`). | `ConsentForm` + `decision` in {allow, deny} | 302 to the client's validated `redirect_uri` with `code` + `state`, or `error=access_denied` + `state`. Uses `HttpResponseRedirect` (https or loopback http only; both were validated at registration and authorize). | Invalid project → re-render 400. Throttled → 429 page. |
| `/connected-apps/` (`mcp_gateway:connections`) | GET | `@login_required` | none | `mcp_gateway/connections.html` (app layout). Context: `connections` (display name, project name, created, last used, status), `mcp_url` (= canonical resource) with how to connect: ChatGPT / Claude via OAuth; Codex / Claude Code via the Connection key header. | none |
| `/connected-apps/<int:grant_id>/revoke/` (`mcp_gateway:revoke`) | POST | `@login_required`, CSRF; owner only, enforced in the service | none (confirm dialog partial) | 302 to `connections` with a message | Not the owner, or unknown → 404 |

Nav: one "Connected apps" link in `templates/partials/nav_links.html` (or only from the Connection key card; see Q5). Project Settings › Connection key card (`templates/partials/connection_key_card.html`): one line saying that regenerating the key also disconnects ChatGPT and Claude connections.

There are no project-scoped views, so `@project_membership_required` does not apply. The service performs the writable-membership check for the chosen project, with the same `WRITABLE_ROLES` source.

### Background work and WebSockets

- `mcp_gateway/tasks.py`: a new beat task `sweep_mcp_oauth` (`notify` queue, hourly at minute 20), with its route and schedule in `settings.py`. It could also be folded into the existing sweep task, but a separate task keeps the failure domains apart.
- No WebSocket, consumer or controller change.

### Throttles (constants in `limits.py`, cache counters through `hit_runner_throttle`, keyed by the `MCP_CLIENT_IP` the ASGI host already sets)

The limits are generous because ChatGPT and Claude call from shared egress IPs.

- `/register`: 30 / min / IP, plus a global 300 / hour. Over either limit → 429 JSON `{"error":"temporarily_unavailable"}` and one warning log.
- `/authorize`: 60 / min / IP. `/token`: 120 / min / IP. `/revoke`: 60 / min / IP.
- Consent POST: 10 / min / user (Django).
- Existing per-IP failure counter on bad bearers: unchanged, and it also counts failed `pha_` lookups.

All of these answer quickly, well inside Claude's 10 s / 30 s timeouts. They involve no outbound I/O, only a few indexed queries.

### Settings / infrastructure

- `auto_tester/settings.py` (python-decouple) and `example.env`:
  - `MCP_OAUTH_ENABLED: bool` (default `False`). When `True`, `SITE_URL` must be set and be https (or http on localhost/127.0.0.1); otherwise `ImproperlyConfigured`. This matches the SDK's `validate_issuer_url`.
  - `MCP_OAUTH_REDIRECT_HOSTS: list[str]` (Csv, default `chatgpt.com,claude.ai,claude.com`). Add `localhost,127.0.0.1` to allow native loopback clients (Codex `mcp login`, Claude Code via OAuth).
- `nginx/server.conf`: the exact-match locations above. Reload nginx.
- `docker-compose.yml`: no change. The `mcp` service already has the env and DB.
- `requirements.txt`: no new dependency.
- `scripts/verify_server_image.sh`: add an import check of `mcp_gateway.oauth_app`.

### Data flow

1. A user adds `https://<host>/mcp` in ChatGPT or claude.ai. The client POSTs `/mcp` without a token. `RequestBodyCap` answers 401 `WWW-Authenticate: Bearer error="invalid_token", …, resource_metadata="https://<host>/.well-known/oauth-protected-resource/mcp", scope="mcp"`.
2. The client GETs the protected-resource metadata (`resource=https://<host>/mcp`, `authorization_servers=["https://<host>"]`, `scopes_supported=["mcp"]`), then `/.well-known/oauth-authorization-server`.
3. `POST /register` (JSON) → throttle → SDK `RegistrationHandler` → `register_client` (allowlist). Response: `client_id` (public for Claude, and ChatGPT DCR).
4. The browser goes to `GET /authorize?client_id&redirect_uri&code_challenge&code_challenge_method=S256&state&scope=mcp&resource=…`. The SDK validates the client, the exact redirect, scope and PKCE. `start_authorization` checks `resource` and stores the pending request. 302 to `https://<host>/connected-apps/authorize/<handle>/`.
5. Django: login if needed. The consent page shows "ChatGPT (chatgpt.com) wants to act as a tester on one of your projects", with a project picker. Allow (POST, CSRF) → `approve` → code (60 s) → 302 `redirect_uri?code&state`.
6. `POST /token` (form) with `grant_type=authorization_code`. The SDK checks the client, the same redirect, PKCE and expiry. `exchange_code` re-checks access, creates the grant and returns access + refresh.
7. The client calls `/mcp` with `Bearer pha_…`. `BearerVerifier` makes one query and returns `McpAccessToken(project_id, user_id, grant_id, resource, scopes)`. The SDK checks the resource and scope. The tool runs through `guarded_call` exactly as for the key (actor "a tester").
8. Expiry → 401 → the client refreshes (`grant_type=refresh_token`): rotation, re-check, new pair.
9. Revoke: the user presses Revoke on `/connected-apps/`, or a member is demoted or removed, the key is regenerated, or the project is archived. The next `/mcp` call → 401, and the next refresh → `invalid_grant`. The client asks the user to reconnect.

## Alternatives considered

- **django-oauth-toolkit in Django.** It gives hashed client secrets, PKCE, refresh rotation, a revocation endpoint, admin, and (3.x) DCR. Rejected:
  - no RFC 8707 resource binding and no RFC 9728 metadata (both required by MCP and used by both clients);
  - the reported `token_endpoint_auth_method` gap would turn Claude's public DCR clients into confidential ones;
  - it brings its own models, views and admin, the generic class-based "framework magic" the project rules avoid;
  - we would still write discovery, the audience check and the MCP verifier bridge ourselves.

  The SDK handlers already implement exactly the MCP profile, and we own a small provider backed by explicit models.
- **SDK `create_auth_routes` via `auth_server_provider=`.** Less code, but its metadata cannot advertise `none` (Claude's CIMD gate needs it; DCR clients may check it). `MCPServer` also refuses `auth_server_provider` together with our custom `token_verifier`. Building routes from the same handler classes costs about 40 lines.
- **OAuth endpoints in Django too (hand-written or oauthlib).** One process, but it reimplements PKCE, client auth and RFC 7591 parsing that the SDK already has.
- **JWT access tokens.** They would avoid a query per request, but we must re-check membership and key fingerprint on every call anyway. Opaque hashed tokens can also be revoked instantly. Rejected.
- **Bind tokens to the user only and pick the project per call.** Every tool would gain a project argument and the gateway would become multi-project. Rejected: a token per project keeps the twelve tools and their DTOs unchanged.
- **Hook `regenerate_api_key` to revoke grants.** That would make `projects` import `mcp_gateway`. The fingerprint compare on read gives the same effect with no reverse import.

## Risks and mitigations

- **Consent phishing.** Anyone can register a client and send a member an authorize link. Mitigations:
  - redirect hosts are allowlisted (codes can only go to chatgpt.com / claude.ai / claude.com by default);
  - the consent page leads with the name derived from the redirect host, not the self-declared name;
  - loopback warning;
  - explicit Allow every time, with no silent re-consent;
  - X-Frame-Options DENY blocks clickjacking.
- **Open redirect.** We redirect only to a URI that matched the client's registered list (SDK exact match) and that passed the allowlist at registration. Error pages for bad handles never redirect.
- **DCR flooding.** Claude registers on every fresh connection. Mitigations: per-IP and global throttles, a global client cap, and a sweep of clients that never got a grant after 24 h. Each client row is small.
- **Token theft or leakage.** Tokens are hashed at rest. Access tokens last 1 h, and refresh tokens rotate with reuse detection that revokes the grant. Tokens are never logged: the uvicorn access log has no headers; `/token` bodies are not logged; `guarded_call` logs only `grant_id`.
- **Logged values.** The consent handle and `state` appear in nginx access logs (GET query or path). The handle is single-use, lasts 10 minutes, and only lets a *logged-in member* consent to send a code to the client's registered redirect, so it is of no use to a log reader. The `code` appears only in the `Location` header of our 302, which is not logged by default.
- **Auth change on a live endpoint.** The Connection key path is moved into `authenticate_bearer.py` unchanged, and `MCP_OAUTH_ENABLED=False` ships first. Regression checks with Codex and Claude Code are in Verification.
- **Audience check now on.** With OAuth enabled, `validate_token_resource=True` refuses any token whose `resource` is not `<SITE_URL>/mcp`, so the verifier must also set `resource` on key tokens. If `SITE_URL` differs from the public host, everything fails. Verification covers it, and settings validation requires https.
- **Resource string mismatch.** Claude requires PRM `resource` to equal the URL the user typed. Document "enter exactly `https://<host>/mcp`". Comparison ignores one trailing slash.
- **`RequestBodyCap` scope bug.** If the bearer pre-check is not narrowed to `/mcp`, discovery and token break. This is an explicit step.
- **Two uvicorn workers.** All provider state is in Postgres and the throttles are in Redis, so there is no per-process state.
- **Encrypted client secrets** depend on `FIELD_ENCRYPTION_KEY`. Rotating that key breaks confidential DCR clients: they get `invalid_client` and re-register. Same exposure as TestRail keys.
- **Query cost.** Each OAuth `/mcp` request makes one verifier query plus the existing project load in `call_guard`. The `last_used_at` write is gated to one per 5 minutes per grant. The connections page is one query.
- **SDK churn.** SDK imports stay confined to `mcp_app.py`, `oauth_app.py`, `oauth_provider.py`, `authenticate_bearer.py` and `mcp_asgi.py`. The handler classes and `OAuthMetadata` are public in 2.3. Keep the `~=2.3.0` pin.

## Implementation steps

1. **Settings, policy, models.** `MCP_OAUTH_ENABLED`, `MCP_OAUTH_REDIRECT_HOSTS` with validation; `oauth_policy.py`, `oauth_tokens.py`, limit constants, the five models and migration `0002_oauth`, and the new exceptions. Shared base for every later step. (files: `auto_tester/settings.py`, `example.env`, `mcp_gateway/{oauth_policy,oauth_tokens,limits,models,exceptions}.py`, `mcp_gateway/migrations/0002_oauth.py`)
2. **Verifier split, with no behaviour change while OAuth is off.** Move the key logic to `authenticate_bearer.py` and add the `pha_` branch and the `McpAccessToken` subclass. Update `project_id_of`, the `call_guard` log label and neutral text, and the `build_mcp_server()` auth settings. (files: `mcp_gateway/services/authenticate_bearer.py` (new), delete `mcp_gateway/services/authenticate_project_key.py`, `mcp_gateway/mcp_app.py`, `mcp_gateway/services/call_guard.py`, `mcp_gateway/services/oauth_access.py`)
3. **Protocol services.** Register, clients, start authorization, exchange, refresh, revoke. (files: `mcp_gateway/services/oauth_{register_client,clients,start_authorization,exchange_code,refresh,revoke}.py`)
4. **SDK adapter, OAuth app and ASGI host.** (files: `mcp_gateway/oauth_provider.py`, `mcp_gateway/oauth_app.py`, `auto_tester/mcp_asgi.py`). Depends on 2 and 3; it can run in parallel with step 5.
5. **Django pages.** Consent and connections services, form, views, urls, templates, nav link and the Connection key card line. (files: `mcp_gateway/services/{oauth_consent,oauth_connections}.py`, `mcp_gateway/{forms,views,urls}.py`, `auto_tester/urls.py`, `templates/mcp_gateway/{consent,connections}.html`, `templates/partials/nav_links.html`, `templates/partials/connection_key_card.html`, `static/css/punk-hazard.css` only if a class is missing)
6. **Sweep.** (files: `mcp_gateway/services/sweep.py`, `mcp_gateway/tasks.py`, `auto_tester/settings.py` beat and route)
7. **Infrastructure.** (files: `nginx/server.conf`, `scripts/verify_server_image.sh`, `.claude/skills/deploy/SKILL.md` for the nginx reload and env vars)
8. **Docs.** CLAUDE.md MCP section (OAuth, settings, import rule, connect instructions) and the change note. (files: `CLAUDE.md`, `docs/changed/<date>-mcp-oauth.md`)

Steps 3, 5 and 6 touch disjoint files once step 1 is in place, so they can go to parallel sub-agents. Step 4 integrates them.

## Verification

Static checks: `mypy .`, `pre-commit run --all-files`, `python manage.py check`, and `python manage.py makemigrations --check --dry-run` after the migration is written.

Manual checks on staging, with `SITE_URL=https://<host>` and `MCP_OAUTH_ENABLED=True`:

- **Discovery.**
  - `curl -i -X POST https://<host>/mcp` → 401 with `resource_metadata` and `scope="mcp"`.
  - `curl` both protected-resource metadata URLs: `resource` equals `https://<host>/mcp` and the issuer matches exactly.
  - `curl` `/.well-known/oauth-authorization-server`: includes `S256`, `none`, `registration_endpoint` and `revocation_endpoint`.
- **DCR.**
  - Register with `redirect_uris=["https://claude.ai/api/mcp/auth_callback"]` → 201 public client.
  - Register with `https://evil.example/cb` → 400 `invalid_redirect_uri`.
  - Register with `http://localhost:9999/cb` → refused unless listed.
  - The 31st registration in a minute from one IP → 429.
- **MCP Inspector OAuth flow end to end:**
  - authorize → login → consent lists only writable projects (a read-only member sees none, and a superuser who is not a member sees none);
  - Allow → tokens → list the 12 tools and call `get_project`, which returns the chosen project;
  - Deny → `error=access_denied` with the state;
  - reusing a code → `invalid_grant`, and the grant is revoked;
  - a wrong `code_verifier` → `invalid_grant`;
  - `resource=https://other/` at authorize → `invalid_target`.
- **Refresh.**
  - Refresh rotates the pair.
  - Replaying the old refresh token after 30 s → `invalid_grant` and the grant is revoked.
  - Replaying it within 30 s → `invalid_grant`, and the grant stays.
- **Revocation and access loss.** Each of the following makes the next tool call 401 and the next refresh `invalid_grant`:
  - Revoke on `/connected-apps/`;
  - demoting the user to read-only, or removing them from the project;
  - regenerating the Connection key;
  - archiving the project;
  - deactivating the user.
- **Real clients.**
  - A ChatGPT developer-mode connector and a claude.ai custom connector (Settings › Connectors › Add custom connector, URL only): connect, consent, claim a Manual case, add a screenshot, mark a step, hand in. The case shows "a tester".
  - Disconnect and reconnect from both.
- **Regression.**
  - Codex CLI (`bearer_token_env_var`) and Claude Code (header) with the Connection key: tools still work.
  - A wrong key → 401, and the per-IP failure counter still logs.
  - With `MCP_OAUTH_ENABLED=False`: no discovery documents, a `pha_` token → 401, and the key works.
- **Logs.** The nginx, uvicorn and app logs contain no access token, refresh token, code or client secret. `guarded_call` lines show `auth=oauth grant=<id>`.
- **Security headers.** The consent page sends `X-Frame-Options: DENY`. A consent POST without CSRF → 403. An expired handle → 400 page with no redirect.

Agents: run `reviewer` (design, queries, layering) and `verifier` (this request + plan path; auth and security focus) in parallel.

## Open questions

- **Q1. Who may approve a connection?** Draft: active members with a writable role only. Read-only members cannot (the tools write). Superusers' implicit admin access is not offered. (Assumption used: writable members only. Alternative: let active superusers pick any project, matching `get_project_access`.)
- **Q2. Should regenerating the Connection key also disconnect every ChatGPT and Claude connection of the project?** Draft: yes, through the fingerprint, so the key stays the project's single kill switch, matching today's "regenerate revokes runners and MCP clients". (Alternative: OAuth grants survive key rotation and are revoked only on the connections page or by losing membership.)
- **Q3. Attribution.** We now know which user approved the connection. Should MCP actions done through OAuth be recorded as that member, for example "Passed manually by Jane" in TestRail and panels, with test-session credit? Or should they stay the neutral "a tester", as the Connection key path does today? Draft: stay "a tester". The user is kept only on the grant and in the gateway log, because changing attribution touches the manual claim, hand-in, TestRail text and test-session hooks. (Assumption used: no attribution change.)
- **Q4. Allowed redirect hosts.** Draft default: `chatgpt.com, claude.ai, claude.com`, with no loopback. Should native clients' loopback redirects (Codex `mcp login`, Claude Code OAuth) be allowed by default, or stay opt-in through `MCP_OAUTH_REDIRECT_HOSTS`? (Assumption used: opt-in; those clients keep using the Connection key header.)
- **Q5. Where users manage connections.** Draft: a personal "Connected apps" page (`/connected-apps/`, nav link) listing only the user's own connections. Should writable members also see and revoke **all** connections of a project on its Settings tab? (Assumption used: personal page only; regenerating the key, Q2, is the project-wide cut.)
- **Q6. Lifetimes.** Draft: access token 1 h; refresh token 30 days idle, rotating, with no absolute cap. Is an indefinitely renewed connection acceptable, or should there be an absolute limit (e.g. 90 days) after which the user approves again? (Assumption used: no absolute cap.)
- **Q7. Rollout switch.** Draft: `MCP_OAUTH_ENABLED` defaults to `False`, and production is switched on after a staging test with both clients. Is an env switch wanted? The existing gateway has no settings by design. (Assumption used: yes, one boolean plus the redirect host list.)
- **Q8. CIMD.** ChatGPT prefers CIMD and the MCP spec says SHOULD. Is DCR-only acceptable for this release, with CIMD as a follow-up plan? (Assumption used: yes.)

## Changelog

- 2026-11-07: created (Draft).
- 2026-11-07: all eight open questions accepted as drafted; implementation started on `feat/mcp-oauth`. Shared plain types for the service/SDK seam live in `mcp_gateway/oauth_types.py` (services never import the SDK).
- 2026-11-07: review round 1 fixed: `/mcp/` routed like `/mcp`; known connector hosts accept only their callback paths and /authorize re-checks the allowlist; per-IP hourly registration cap (20) and unused clients swept after 1 h; `/token` refuses a foreign `resource`; sweep keeps expired connections listed for the history window and logs per-model counts; duplicate indexes dropped; `last_used_at` throttled from the loaded value (no Redis round trip); `revoke_grant` moved to `oauth_revoke`; `deny` takes no user; `AuthKind` literal; consent wording ("a tester") and full redirect URI shown; settings parse the SITE_URL host; nginx uses one anchored regex location.
- Deferred (reviewer nit): moving `hit_runner_throttle` to a neutral leaf module and the OAuth throttle policy out of `auto_tester/mcp_asgi.py` into `mcp_gateway/oauth_throttle.py`. It is a cross-app refactor of an existing helper used by runner code; left for a separate plan.
- 2026-11-07: review round 2 nits fixed: `McpGrant.user` has no separate index (covered by `mcp_grant_user_idx`), docs say "one anchored regex location". Where the Design section above still says "exact-match locations" (nginx) or "clients with no grant that are more than 24 h old" (sweep, risks), the implemented behaviour is one anchored regex location and clients with no authorization code after `MCP_OAUTH_UNUSED_CLIENT_SECONDS` (1 h).
