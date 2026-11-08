"""How the test machine words a command session it no longer runs.

Shared by the command tools (``command_sessions``) and the claim-end stop
(``stop_claim_commands``): both forget a registered session only when the
test machine's error reply says it is gone, never on a timeout or another
fault.
"""

from __future__ import annotations

import re
from typing import Final

# The error text for a session id the test machine does not know
# (foreground and kept sessions).
_UNKNOWN_SESSION: Final = re.compile(r"no (?:active |kept )?session", re.IGNORECASE)
# An older test machine's foreground session past its 300-second limit: it
# is terminated, and every later read answers this.
_SESSION_TIMED_OUT: Final = re.compile(
    r"exceeded overall timeout|session timed out", re.IGNORECASE
)


def is_unknown_session(text: str) -> bool:
    """The test machine does not know the session."""
    return _UNKNOWN_SESSION.search(text) is not None


def is_timed_out_session(text: str) -> bool:
    """The test machine ended the session at its time limit."""
    return _SESSION_TIMED_OUT.search(text) is not None
