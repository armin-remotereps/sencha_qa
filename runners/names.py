"""Validation of a runner's human-given name (shared by create and rename)."""

from __future__ import annotations

import unicodedata

from runners.exceptions import RunnerNameInvalidError
from runners.models import RUNNER_NAME_MAX_LENGTH


def normalize_runner_name(raw: str) -> str:
    """The stripped name; raises ``RunnerNameInvalidError`` when blank, too long
    or holding control, format or other invisible characters."""
    name = raw.strip()
    if not name:
        raise RunnerNameInvalidError("Enter a name for the Test Runner.")
    if len(name) > RUNNER_NAME_MAX_LENGTH:
        raise RunnerNameInvalidError(
            f"Use at most {RUNNER_NAME_MAX_LENGTH} characters."
        )
    if any(unicodedata.category(char).startswith("C") for char in name):
        raise RunnerNameInvalidError("The name contains invisible characters.")
    return name
