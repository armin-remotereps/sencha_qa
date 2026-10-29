"""Who may choose a run's help mode: superusers only."""

from __future__ import annotations

from accounts.models import CustomUser
from projects.exceptions import HelpModeNotAllowedError


def resolve_help_mode(
    *, user: CustomUser, requested: str | None, current: str
) -> str | None:
    """The mode to store, or ``None`` to keep ``current``.

    A non-superuser may only keep the stored mode; asking for another one raises
    ``HelpModeNotAllowedError``. Call it with the locked run's mode, before any
    write, after the requested value was validated.
    """
    if user.is_superuser:
        return requested
    if requested is None or requested == current:
        return None
    raise HelpModeNotAllowedError()
