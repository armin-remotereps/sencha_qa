"""Runner token minting and hashing (the one definition of a runner token).

The plaintext is returned once to the owner and stored only as a sha256 hash
(lookup) plus Fernet ciphertext (ZIP re-download). Never log it, never put it
in a URL or in Celery task args.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Final

from projects.secrets import encrypt_secret
from runners.types import IssuedRunnerToken

# Recognisable in a leaked ``.env`` or log; OAuth access tokens use ``pha_``.
RUNNER_TOKEN_PREFIX: Final = "phr_"
_TOKEN_RANDOM_BYTES: Final = 32


def hash_runner_token(plaintext: str) -> str:
    """sha256 hex of the token (what ``TestRunner.token_hash`` stores)."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def mint_runner_token() -> IssuedRunnerToken:
    plaintext = RUNNER_TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_RANDOM_BYTES)
    return IssuedRunnerToken(
        plaintext=plaintext,
        token_hash=hash_runner_token(plaintext),
        token_encrypted=encrypt_secret(plaintext),
    )
