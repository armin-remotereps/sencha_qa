# Local stub for pywebpush 2.5 (ships no type information). Covers only what
# notifications/services/send_push.py uses: the synchronous ``webpush`` call
# and ``WebPushException``.
from collections.abc import Mapping
from typing import Protocol

class _Response(Protocol):
    @property
    def status_code(self) -> int: ...
    @property
    def text(self) -> str: ...

class WebPushException(Exception):
    message: str
    # None when the request never got an HTTP response (connection error).
    response: _Response | None
    def __init__(self, message: str, response: _Response | None = None) -> None: ...
    @property
    def status_code(self) -> int | None: ...
    @property
    def retry_after(self) -> str | None: ...

def webpush(
    subscription_info: Mapping[str, object],
    data: str | bytes | None = None,
    vapid_private_key: str | None = None,
    vapid_claims: dict[str, str | int] | None = None,
    content_encoding: str = "aes128gcm",
    curl: bool = False,
    timeout: float | None = None,
    ttl: int = 0,
    verbose: bool = False,
    headers: dict[str, str | int | float] | None = None,
) -> _Response: ...  # a curl command string only with curl=True (unused)
