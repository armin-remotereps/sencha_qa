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
