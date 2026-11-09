"""Reading the ``modifiers`` argument of the desktop pointer tools.

Pure: no ORM, imports only the controller protocol, so ``projects`` and
``mcp_gateway`` may use it without the ``agents.services`` import cycle. The
canonical names are ``PointerModifier``'s; models drift, so a few aliases are
accepted (``cmd`` means ``primary``: Cmd on macOS, Ctrl elsewhere).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from controller_client.protocol import POINTER_MODIFIER_ORDER, PointerModifier

MAX_POINTER_MODIFIERS: Final[int] = len(POINTER_MODIFIER_ORDER)

_ALIASES: Final[dict[str, PointerModifier]] = {
    **{m.value: m for m in PointerModifier},
    "control": PointerModifier.CTRL,
    "option": PointerModifier.ALT,
    "opt": PointerModifier.ALT,
    "cmd": PointerModifier.PRIMARY,
    "command": PointerModifier.PRIMARY,
    "win": PointerModifier.META,
    "windows": PointerModifier.META,
    "super": PointerModifier.META,
}

_ALLOWED_TEXT: Final[str] = ", ".join(m.value for m in POINTER_MODIFIER_ORDER)


class PointerModifierArgumentError(ValueError):
    """The ``modifiers`` argument is not a list of known modifier names."""


def read_pointer_modifiers(raw: object) -> tuple[PointerModifier, ...]:
    """The modifiers in press order; ``None`` or ``[]`` is ``()``."""
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(m, str) for m in raw):
        raise PointerModifierArgumentError(
            f"modifiers must be a list of names: {_ALLOWED_TEXT}."
        )
    if len(raw) > MAX_POINTER_MODIFIERS:
        raise PointerModifierArgumentError(
            f"modifiers takes at most {MAX_POINTER_MODIFIERS} names: {_ALLOWED_TEXT}."
        )
    found: set[PointerModifier] = set()
    for name in raw:
        modifier = _ALIASES.get(name.strip().lower())
        if modifier is None:
            raise PointerModifierArgumentError(
                f"Unknown modifier {name.strip()[:20]!r}; use {_ALLOWED_TEXT}."
            )
        found.add(modifier)
    return tuple(m for m in POINTER_MODIFIER_ORDER if m in found)


def modifiers_text(modifiers: Sequence[PointerModifier]) -> str:
    """``"shift+primary"`` style text, ``""`` without modifiers."""
    return "+".join(m.value for m in modifiers)
