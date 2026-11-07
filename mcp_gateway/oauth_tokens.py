"""Stateless helpers for OAuth token-like values (no ORM).

Access and refresh tokens carry a readable prefix so the bearer verifier
knows which table to try; codes and consent handles have none. Every value is
256 bits of randomness, so a fast sha256 digest is the right stored form.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Final

ACCESS_TOKEN_PREFIX: Final = "pha_"
REFRESH_TOKEN_PREFIX: Final = "phr_"


def mint_token(prefix: str = "") -> str:
    """A new random value (256 bits, urlsafe) with ``prefix``."""
    return prefix + secrets.token_urlsafe(32)


def hash_token(value: str) -> str:
    """The stored form of a token-like value: sha256, lowercase hex."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def api_key_fingerprint(api_key: str) -> str:
    """The fingerprint of a project's Connection key kept on a grant."""
    return hash_token(api_key)
