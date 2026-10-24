"""Which tool set and prompt a setup turn gets (pure)."""

from __future__ import annotations

from enum import StrEnum


class ThreadRole(StrEnum):
    # The project's Machine setup chat (and person turns of a thread whose
    # help request is gone).
    MAIN = "main"
    # The unattended Night auto-fix turn: ``report_case_fix`` only.
    AUTO_FIX = "auto_fix"
    # A person's turn in a help request's thread: the help-chat tools.
    CASE_HELP = "case_help"


def thread_role(*, auto_fix_turn: bool, help_request_id: int | None) -> ThreadRole:
    if auto_fix_turn:
        return ThreadRole.AUTO_FIX
    if help_request_id is not None:
        return ThreadRole.CASE_HELP
    return ThreadRole.MAIN
