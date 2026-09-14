from __future__ import annotations

from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured


def validate_fernet_key(key: str) -> str:
    """Return `key` unchanged if Fernet accepts it; raise ImproperlyConfigured otherwise."""
    try:
        Fernet(key.encode("ascii"))
    except (ValueError, TypeError, UnicodeEncodeError) as exc:
        raise ImproperlyConfigured(
            "FIELD_ENCRYPTION_KEY must be a urlsafe base64-encoded 32-byte Fernet key. "
            'Generate one with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        ) from exc
    return key
