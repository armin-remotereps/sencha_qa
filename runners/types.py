"""Plain types of the Test Runner domain (a leaf: no Django model imports).

``RunnerId`` is the strongly typed runner id: every ``controller_*`` call,
``wait_for_controller`` and ``agents.types.ToolContext.runner_id`` take it, so
mypy strict rejects a plain ``int`` (such as a project id) at the call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NewType

RunnerId = NewType("RunnerId", int)


@dataclass(frozen=True)
class IssuedRunnerToken:
    """A freshly minted token: the plaintext (shown once) and what is stored."""

    plaintext: str
    token_hash: str
    token_encrypted: str
