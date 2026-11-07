"""Operational limits of the MCP gateway (constants, not settings: the
gateway is always on and has no configuration)."""

from __future__ import annotations

from typing import Final

from django.conf import settings

# Request body cap, checked before parsing (nginx caps at 11m as well).
MCP_REQUEST_MAX_BYTES: Final[int] = 10 * 1024 * 1024
MCP_READ_RATE_PER_MINUTE: Final[int] = 120
MCP_MARK_RATE_PER_MINUTE: Final[int] = 60
# Failed key checks per client IP and minute: counted (and logged once when
# crossed), never used to refuse a valid key (shared egress IPs).
MCP_AUTH_FAILURES_PER_MINUTE: Final[int] = 20
# Staged (not yet handed in) screenshots per claim. Staging rows live as long
# as their claim: no TTL (the sweep deletes them once the claim ends).
MCP_STAGED_SHOTS_MAX: Final[int] = 60
MCP_STAGED_BYTES_MAX: Final[int] = 100 * 1024 * 1024
# Longest ``image_base64`` accepted (padded base64 of the manual screenshot
# byte cap); the tool schema and the service both use it, so a longer string
# is refused before any decoding.
MCP_SHOT_MAX_BASE64_CHARS: Final[int] = 4 * -(-settings.MANUAL_SHOT_MAX_BYTES // 3)

# --- Runner capture (list_windows, capture_screenshot;
# docs/plan/feat-mcp-runner-capture.md) ---
# list_windows calls per project and minute (capture_screenshot counts
# against the screenshot budget shared with add_screenshot).
MCP_MACHINE_RATE_PER_MINUTE: Final[int] = 30
# Longest wait for the test machine's capture reply. The runner's own
# budgets add up to about 34 s in the worst case (hide 2 s, desktop switch
# 6 s, settle 0.5 s, delay_ms 5 s, freshness 3 s, grab and encode 6 s,
# switch back 6 s, plus the screen lock), plus the upload of at most
# MANUAL_SHOT_MAX_BYTES; 45 s leaves room for the decode before the deadline.
MCP_CAPTURE_REPLY_TIMEOUT_SECONDS: Final[int] = 45
# A capture call gives up (``[machine_busy]``, nothing staged) when it cannot
# get a decode slot this long after it started: under a 60 s client timeout,
# so a staged shot always reaches the client with its shot_id.
MCP_CAPTURE_DEADLINE_SECONDS: Final[int] = 55
# Test machine round trips (list_windows, capture_screenshot) waiting at
# once in one gateway process: each holds a worker thread for up to the
# reply timeout, so more are refused (``[machine_busy]``) instead of queued.
MCP_MACHINE_ROUND_TRIPS_MAX: Final[int] = 8
MCP_LIST_WINDOWS_TIMEOUT_SECONDS: Final[int] = 10
# One capture per project at a time (cache lock); outlives the reply wait.
MCP_CAPTURE_LOCK_SECONDS: Final[int] = 60
# Socket timeouts of the capture lock's Redis client: an unreachable Redis
# must not hold a worker thread past the call budget.
MCP_CAPTURE_LOCK_REDIS_TIMEOUT_SECONDS: Final[float] = 2.0
# The preview returned as image content (never stored).
MCP_CAPTURE_PREVIEW_MAX_EDGE: Final[int] = 1280
MCP_CAPTURE_PREVIEW_MAX_BYTES: Final[int] = 400 * 1024
MCP_CAPTURE_PREVIEW_JPEG_QUALITY: Final[int] = 70

# --- OAuth 2.1 (mcp_gateway/oauth_*; docs/plan/feat-mcp-oauth.md) ---
MCP_OAUTH_ACCESS_TTL_SECONDS: Final[int] = 3600
# Rotating: an active connection lives on, an idle one dies after this.
MCP_OAUTH_REFRESH_TTL_SECONDS: Final[int] = 30 * 86400
MCP_OAUTH_CODE_TTL_SECONDS: Final[int] = 60
MCP_OAUTH_CONSENT_TTL_SECONDS: Final[int] = 600
# A rotated refresh token replayed within this window is refused without
# revoking the grant (Claude's proactive and reactive refreshes can race).
MCP_OAUTH_REFRESH_REUSE_GRACE_SECONDS: Final[int] = 30
# Live dynamically registered clients; registration is refused above it.
MCP_OAUTH_MAX_CLIENTS: Final[int] = 5000
# Throttles (per client IP and minute unless named otherwise). Generous:
# ChatGPT and Claude call from shared egress IPs.
MCP_OAUTH_REGISTER_PER_MINUTE: Final[int] = 30
MCP_OAUTH_REGISTER_PER_HOUR_GLOBAL: Final[int] = 300
# Per IP and hour, so one caller cannot use up the global budget.
MCP_OAUTH_REGISTER_PER_HOUR_PER_IP: Final[int] = 20
# A client that got no authorization code this long after registering is
# swept (Claude registers on every fresh connection).
MCP_OAUTH_UNUSED_CLIENT_SECONDS: Final[int] = 3600
MCP_OAUTH_AUTHORIZE_PER_MINUTE: Final[int] = 60
MCP_OAUTH_TOKEN_PER_MINUTE: Final[int] = 120
MCP_OAUTH_REVOKE_PER_MINUTE: Final[int] = 60
# Consent page POSTs per user and minute (Django).
MCP_OAUTH_CONSENT_PER_MINUTE: Final[int] = 10
# Body cap of the OAuth endpoints (nginx caps at 64k as well).
MCP_OAUTH_REQUEST_MAX_BYTES: Final[int] = 64 * 1024
# The verifier marks a grant used at most once per this many seconds.
MCP_OAUTH_LAST_USED_EVERY_SECONDS: Final[int] = 300
# Revoked or expired connections stay listed on the page this long.
MCP_OAUTH_CONNECTIONS_HISTORY_DAYS: Final[int] = 30
