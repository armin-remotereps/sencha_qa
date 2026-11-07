"""Use case: load a registered OAuth client for the SDK (``get_client``).

One indexed query on ``client_id``. The secret is decrypted here; a secret
that no longer decrypts (``FIELD_ENCRYPTION_KEY`` rotated) answers None like
an unknown or swept client, so the token endpoint says ``invalid_client``
and the client registers again.
"""

from __future__ import annotations

import logging
from typing import Final

from mcp_gateway.models import McpOAuthClient
from mcp_gateway.oauth_types import StoredClient
from projects.secrets import SecretDecryptionError, decrypt_secret

logger = logging.getLogger(__name__)

_CLIENT_ID_MAX_CHARS: Final = 64


def load_client(client_id: str) -> StoredClient | None:
    """The client with ``client_id``, or None when unknown or unusable."""
    if not client_id or len(client_id) > _CLIENT_ID_MAX_CHARS:
        return None
    row = (
        McpOAuthClient.objects.filter(client_id=client_id)
        .values_list("pk", "metadata", "client_secret_encrypted")
        .first()
    )
    if row is None:
        return None
    pk, metadata, encrypted = row
    secret: str | None = None
    if encrypted:
        try:
            secret = decrypt_secret(encrypted)
        except SecretDecryptionError:
            logger.warning("mcp oauth client %s secret cannot be decrypted", pk)
            return None
    if not isinstance(metadata, dict):
        return None
    return StoredClient(metadata=dict(metadata), client_secret=secret)
