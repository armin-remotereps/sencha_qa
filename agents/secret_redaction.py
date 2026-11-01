"""Replace obvious secret values in model-written text with ``[redacted]``.

Pure, no ORM. A net under the prompt rule "never copy a secret out of a
reference" for Human in the loop guides: it catches the common shapes
(``password=…``, ``"token": "…"``, ``Bearer …``, ``user:pass@`` in URLs and
connection strings, PEM private key blocks, AWS access key ids), not every
possible secret.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Final

REDACTED: Final = "[redacted]"
# A shorter value is redacted even when the case states it ("1" in "step 1").
KNOWN_VALUE_MIN_CHARS: Final = 4

_Known = Callable[[str], bool]
_Replacer = Callable[[re.Match[str]], str]

# "password: x", "DB_PASSWORD=x", '"api_key": "x"', "--token=x", "Pwd=x;".
# The key word starts the key or follows a "_", "." or "-" and ends right at
# the separator (optional closing quote and spaces): "--token-file=" and
# "max_tokens=" are not secrets.
_KEY_VALUE: Final = re.compile(
    r"(?P<key>(?<![\w.-])(?:[\w.-]*[_.-])?"
    r"(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"client[_-]?secret|secret[_-]?key|private[_-]?key)"
    r"[\"']?[ \t]*[:=][ \t]*)"
    r"(?:(?P<quote>[\"'])(?P<quoted>[^\"'\n]*)(?P=quote)"
    r"|(?P<placeholder><[^<>\n]*>)"
    r"|(?P<bare>[^\s\"',;]+))",
    re.IGNORECASE,
)
_BEARER: Final = re.compile(r"\b(Bearer[ \t]+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)
# scheme://user:password@host -> scheme://user:[redacted]@host
_URL_CREDENTIALS: Final = re.compile(
    r"(?P<prefix>\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)(?P<password>[^\s@/]+)@",
    re.IGNORECASE,
)
_PEM_PRIVATE_KEY: Final = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_AWS_ACCESS_KEY_ID: Final = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
# Words that follow "Password:" in prose rather than a value ("Password: see
# «Test accounts.xlsx»"), and placeholders that hold no secret.
_NOT_A_VALUE: Final = frozenset(
    {
        "see", "in", "is", "are", "the", "a", "an", "stored", "kept", "from",
        "none", "n/a", "na", "not", "required", "empty", "hidden", "ask",
        "provided", "given", "set", "unset", "your", "their", "same", "as",
        "redacted", "[redacted]", "***", "****", "*****", "xxx", "<password>",
    }
)  # fmt: skip
_PLACEHOLDER: Final = re.compile(r"^(?:<[^>]*>|%[\w]+%|\$\{?[\w]+\}?|\{[\w]+\})$")


def redact_secrets(text: str, *, known_text: str = "") -> str:
    """``text`` with obvious secret values replaced by ``[redacted]``.

    A value that appears verbatim in ``known_text`` (the test case itself) is
    kept: the case may state the value a tester has to type. It must be at
    least ``KNOWN_VALUE_MIN_CHARS`` long and stand there as a whole token.
    """
    if not text:
        return text

    def known(value: str) -> bool:
        return _stated_in(value, known_text)

    def key_value(match: re.Match[str]) -> str:
        if match.group("placeholder"):
            # "password=<your password>": a placeholder, no secret.
            return match.group(0)
        quote = match.group("quote")
        value = match.group("quoted") if quote else match.group("bare")
        if (
            not value
            or value.lower() in _NOT_A_VALUE
            or _PLACEHOLDER.match(value)
            or value.startswith("«")
            or known(value)
        ):
            return match.group(0)
        if quote:
            return f"{match.group('key')}{quote}{REDACTED}{quote}"
        return f"{match.group('key')}{REDACTED}"

    def url_credentials(match: re.Match[str]) -> str:
        if known(match.group("password")):
            return match.group(0)
        return f"{match.group('prefix')}{REDACTED}@"

    text = _PEM_PRIVATE_KEY.sub(_keep_known_block(known), text)
    text = _URL_CREDENTIALS.sub(url_credentials, text)
    text = _BEARER.sub(_bearer(known), text)
    text = _KEY_VALUE.sub(key_value, text)
    return _AWS_ACCESS_KEY_ID.sub(_keep_known_block(known), text)


def _keep_known_block(known: _Known) -> _Replacer:
    def replace(match: re.Match[str]) -> str:
        return match.group(0) if known(match.group(0)) else REDACTED

    return replace


def _bearer(known: _Known) -> _Replacer:
    def replace(match: re.Match[str]) -> str:
        token = match.group(0)[len(match.group(1)) :]
        return match.group(0) if known(token) else f"{match.group(1)}{REDACTED}"

    return replace


def _stated_in(value: str, known_text: str) -> bool:
    """Whether ``value`` stands in ``known_text`` as a whole token."""
    if len(value) < KNOWN_VALUE_MIN_CHARS or not known_text:
        return False
    pattern = rf"(?<![\w]){re.escape(value)}(?![\w])"
    return re.search(pattern, known_text) is not None
