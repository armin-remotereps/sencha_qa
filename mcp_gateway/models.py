"""Staging rows of the MCP gateway (internal, never rendered).

While an MCP tester performs a claimed Manual run case, its screenshots and
step marks live here until the hand-in turns them into the ordinary manual
records (``ManualCaseSubmission``, ``TestRunScreenshot``). Rows are scoped to
one claim (``pivot`` + ``claim_token``) and deleted at hand-in and release,
and by the hourly sweep once the claim no longer matches (never while it is
live).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from controller_client.protocol import MANUAL_SHOT_KEY_MAX_CHARS
from projects.models import MANUAL_CLAIM_TOKEN_LENGTH, Project, TestRunTestCase


class McpStagedShot(models.Model):
    """One screenshot the MCP tester sent for a claimed case (validated)."""

    pivot = models.ForeignKey(
        TestRunTestCase, on_delete=models.CASCADE, related_name="+"
    )
    claim_token = models.CharField(max_length=MANUAL_CLAIM_TOKEN_LENGTH)
    # Server-minted, the protocol's screenshot key format; the tool's shot_id.
    shot_key = models.CharField(max_length=MANUAL_SHOT_KEY_MAX_CHARS)
    # ``ManualShotMime`` value.
    mime = models.CharField(max_length=16)
    size = models.PositiveIntegerField()
    # Lowercase hex of ``data``.
    sha256 = models.CharField(max_length=64)
    data = models.BinaryField()
    # Null until a mark binds it to a step (or it is the case screenshot).
    step_num = models.PositiveSmallIntegerField(null=True, blank=True)
    # ``ManualShotKind`` of a bound shot ("pass" / "fail"); blank while unbound.
    kind = models.CharField(max_length=8, blank=True, default="")
    bound_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    # ``projects.models.ScreenshotCaptureSource`` value: "runner" for
    # ``capture_screenshot``, "" for ``add_screenshot``; copied to the stored
    # screenshot at hand-in. Internal. Never render a value that is not
    # ``runner``.
    capture_source = models.CharField(max_length=16, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["pivot", "shot_key"], name="mcp_staged_shot_key_uniq"
            )
        ]
        indexes = [
            models.Index(
                fields=["pivot", "claim_token"], name="mcp_staged_shot_claim_idx"
            )
        ]

    def __str__(self) -> str:
        return f"McpStagedShot #{self.pk}"


class McpStepMark(models.Model):
    """The MCP tester's mark of one step of a claimed case (Pass / Fail).

    The hand-in is built from these rows and the bound shots; the advisory
    ``ManualCaseProgress`` row is only kept in step for the web card.
    """

    pivot = models.ForeignKey(
        TestRunTestCase, on_delete=models.CASCADE, related_name="+"
    )
    claim_token = models.CharField(max_length=MANUAL_CLAIM_TOKEN_LENGTH)
    step_num = models.PositiveSmallIntegerField()
    # ``ManualStepOutcome`` value.
    outcome = models.CharField(max_length=8)
    note = models.TextField(blank=True, default="")
    # Time spent on the step, capped at MANUAL_STEP_MAX_SECONDS.
    seconds = models.PositiveIntegerField(default=0)
    marked_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["pivot", "claim_token", "step_num"],
                name="mcp_step_mark_step_uniq",
            )
        ]

    def __str__(self) -> str:
        return f"McpStepMark #{self.pk}"


# --- OAuth 2.1 (docs/plan/feat-mcp-oauth.md) ---------------------------------
# Every token-like value (access/refresh token, code, consent handle) is a
# 256-bit random string; only its sha256 hex digest is stored
# (``mcp_gateway.oauth_tokens``). Client secrets are Fernet-encrypted, not
# hashed: the SDK's client authenticator compares the plaintext.

_HASH_CHARS = 64
_STATE_MAX_CHARS = 512
_URI_MAX_CHARS = 2000


class OAuthTokenEndpointAuthMethod(models.TextChoices):
    NONE = "none", "Public client"
    CLIENT_SECRET_POST = "client_secret_post", "Client secret (POST)"
    CLIENT_SECRET_BASIC = "client_secret_basic", "Client secret (Basic)"


class OAuthTokenKind(models.TextChoices):
    ACCESS = "access", "Access"
    REFRESH = "refresh", "Refresh"


class OAuthGrantRevokedReason(models.TextChoices):
    USER = "user", "Revoked by the user"
    CLIENT = "client", "Revoked by the app"
    REFRESH_REUSE = "refresh_reuse", "Refresh token reused"
    CODE_REUSE = "code_reuse", "Authorization code reused"
    MEMBERSHIP = "membership", "Access to the project ended"


class McpOAuthClient(models.Model):
    """One dynamically registered OAuth client (RFC 7591)."""

    client_id = models.CharField(max_length=64, unique=True)
    # Self-declared and untrusted (control characters stripped, <= 100 chars).
    client_name = models.CharField(max_length=100, blank=True, default="")
    # Exact redirect URIs, each allowed by ``oauth_policy.redirect_uri_allowed``.
    redirect_uris = models.JSONField(default=list)
    token_endpoint_auth_method = models.CharField(
        max_length=24, choices=OAuthTokenEndpointAuthMethod.choices
    )
    # Fernet (projects.secrets); blank for public clients.
    client_secret_encrypted = models.TextField(blank=True, default="")
    # 0 = never expires.
    client_secret_expires_at = models.BigIntegerField(default=0)
    scope = models.CharField(max_length=64, default="mcp")
    # The validated RFC 7591 metadata minus the secret, to rebuild
    # ``OAuthClientInformationFull``.
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    # Null until the first authorization code is issued to it.
    last_used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"McpOAuthClient #{self.pk}"


class McpAuthorizationRequest(models.Model):
    """A pending consent created by /authorize; single use, short-lived."""

    handle_hash = models.CharField(max_length=_HASH_CHARS, unique=True)
    client = models.ForeignKey(
        McpOAuthClient, on_delete=models.CASCADE, related_name="+"
    )
    redirect_uri = models.CharField(max_length=_URI_MAX_CHARS)
    redirect_uri_provided_explicitly = models.BooleanField(default=True)
    code_challenge = models.CharField(max_length=128)
    state = models.CharField(max_length=_STATE_MAX_CHARS, blank=True, default="")
    # True when the client sent a ``state`` (even an empty one).
    has_state = models.BooleanField(default=False)
    scopes = models.JSONField(default=list)
    resource = models.CharField(max_length=_URI_MAX_CHARS)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"McpAuthorizationRequest #{self.pk}"


class McpGrant(models.Model):
    """One connection: a client acting for one user on one project."""

    client = models.ForeignKey(
        McpOAuthClient, on_delete=models.CASCADE, related_name="grants"
    )
    # Indexed by ``mcp_grant_user_idx`` (leading column).
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="+",
        db_index=False,
    )
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="+")
    scopes = models.JSONField(default=list)
    resource = models.CharField(max_length=_URI_MAX_CHARS)
    # sha256 hex of ``Project.api_key`` at consent: regenerating the key ends
    # every connection of the project (compared on read).
    api_key_fingerprint = models.CharField(max_length=_HASH_CHARS)
    created_at = models.DateTimeField(default=timezone.now)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.CharField(
        max_length=16, choices=OAuthGrantRevokedReason.choices, blank=True, default=""
    )

    class Meta:
        indexes = [
            models.Index(fields=["user", "revoked_at"], name="mcp_grant_user_idx"),
        ]

    def __str__(self) -> str:
        return f"McpGrant #{self.pk}"


class McpAuthorizationCode(models.Model):
    """An authorization code issued on approval; single use, 60 seconds."""

    code_hash = models.CharField(max_length=_HASH_CHARS, unique=True)
    client = models.ForeignKey(
        McpOAuthClient, on_delete=models.CASCADE, related_name="+"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+"
    )
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="+")
    code_challenge = models.CharField(max_length=128)
    redirect_uri = models.CharField(max_length=_URI_MAX_CHARS)
    redirect_uri_provided_explicitly = models.BooleanField(default=True)
    resource = models.CharField(max_length=_URI_MAX_CHARS)
    scopes = models.JSONField(default=list)
    expires_at = models.DateTimeField(db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    # The grant the exchange created (revoked if the code is replayed).
    grant = models.ForeignKey(
        McpGrant, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    def __str__(self) -> str:
        return f"McpAuthorizationCode #{self.pk}"


class McpOAuthToken(models.Model):
    """An access or refresh token of a grant (only its hash is stored)."""

    token_hash = models.CharField(max_length=_HASH_CHARS, unique=True)
    kind = models.CharField(max_length=8, choices=OAuthTokenKind.choices)
    # Indexed by ``mcp_token_grant_kind_idx`` (leading column).
    grant = models.ForeignKey(
        McpGrant, on_delete=models.CASCADE, related_name="tokens", db_index=False
    )
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(default=timezone.now)
    # Refresh only: set when exchanged for a new pair.
    rotated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["grant", "kind"], name="mcp_token_grant_kind_idx"),
        ]

    def __str__(self) -> str:
        return f"McpOAuthToken #{self.pk}"
