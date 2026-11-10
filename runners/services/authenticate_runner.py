"""Resolve a controller handshake's ``runner_token`` to its runner."""

from __future__ import annotations

import hmac

from django.conf import settings

from runners.models import TestRunner
from runners.tokens import hash_runner_token


def authenticate_runner(token: str) -> TestRunner | None:
    """The runner the token belongs to, or None (one indexed query).

    Refused (None) for an unknown token, an archived runner or an inactive
    owner; the caller answers every refusal with the same text so a probe
    learns nothing. The token is never logged.
    """
    if not token:
        return None
    token_hash = hash_runner_token(token)
    runner = (
        TestRunner.objects.select_related("owner")
        .filter(
            token_hash=token_hash,
            archived_at__isnull=True,
            owner__is_active=True,
        )
        .first()
    )
    if runner is None or not hmac.compare_digest(runner.token_hash, token_hash):
        return None
    return runner


def outdated_runner_message() -> str:
    """The handshake refusal for a runner without a token or USER_RUNNER_V1
    (answered with ``status="incompatible"``, which old runners treat as
    terminal)."""
    site = settings.SITE_URL.strip().rstrip("/")
    where = f"{site}/runners/" if site else "the Test Runners page"
    return f"This Test Runner is outdated. Install a new one from {where}"
