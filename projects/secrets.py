from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


class SecretDecryptionError(Exception):
    """Raised when a stored secret cannot be decrypted with the configured key."""


def _fernet() -> Fernet:
    return Fernet(settings.FIELD_ENCRYPTION_KEY.encode("ascii"))


def encrypt_secret(value: str) -> str:
    """Encrypt a plaintext secret into a urlsafe Fernet token for storage."""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    """Decrypt a Fernet token produced by encrypt_secret."""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise SecretDecryptionError(
            "Stored secret cannot be decrypted with the current FIELD_ENCRYPTION_KEY."
        ) from exc
