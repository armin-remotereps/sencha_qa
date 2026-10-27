"""Map pyautogui-style key names and characters to X keysyms.

Pure tables, no I/O. The GNOME backend sends keys to Mutter's RemoteDesktop
``NotifyKeyboardKeysym``, which takes X keysyms (values from xkbcommon's
``xkbcommon-keysyms.h`` / X11 ``keysymdef.h`` and ``XF86keysym.h``). Key names
are the ones the agent already sends to pyautogui, with the same Linux aliases
as the non-macOS ``_KEY_ALIASES`` in ``desktop_pyautogui.py``.
"""

from __future__ import annotations

from typing import Final

_KEYSYM_RETURN: Final = 0xFF0D
_KEYSYM_TAB: Final = 0xFF09
_KEYSYM_F1: Final = 0xFFBE
_UNICODE_KEYSYM_OFFSET: Final = 0x01000000

# Alias -> canonical name. Canonical names are the keys of _NAMED_KEYSYMS.
_KEY_ALIASES: Final[dict[str, str]] = {
    # Linux modifier aliases, as desktop_pyautogui._KEY_ALIASES (non-macOS).
    "cmd": "win",
    "command": "win",
    "meta": "win",
    "super": "win",
    "opt": "alt",
    "option": "alt",
    "optionleft": "altleft",
    "optionright": "altright",
    "control": "ctrl",
    # pyautogui spellings.
    "return": "enter",
    "escape": "esc",
    "del": "delete",
    "pgup": "pageup",
    "pgdn": "pagedown",
    "prtsc": "printscreen",
    "prtscr": "printscreen",
    "prntscrn": "printscreen",
    "print": "printscreen",
    "apps": "menu",
}

_NAMED_KEYSYMS: Final[dict[str, int]] = {
    "enter": _KEYSYM_RETURN,  # Return
    "esc": 0xFF1B,  # Escape
    "tab": _KEYSYM_TAB,  # Tab
    "backspace": 0xFF08,  # BackSpace
    "delete": 0xFFFF,  # Delete
    "insert": 0xFF63,  # Insert
    "space": 0x0020,  # space
    "left": 0xFF51,  # Left
    "up": 0xFF52,  # Up
    "right": 0xFF53,  # Right
    "down": 0xFF54,  # Down
    "home": 0xFF50,  # Home
    "end": 0xFF57,  # End
    "pageup": 0xFF55,  # Prior / Page_Up
    "pagedown": 0xFF56,  # Next / Page_Down
    "ctrl": 0xFFE3,  # Control_L
    "ctrlleft": 0xFFE3,  # Control_L
    "ctrlright": 0xFFE4,  # Control_R
    "shift": 0xFFE1,  # Shift_L
    "shiftleft": 0xFFE1,  # Shift_L
    "shiftright": 0xFFE2,  # Shift_R
    "alt": 0xFFE9,  # Alt_L
    "altleft": 0xFFE9,  # Alt_L
    "altright": 0xFFEA,  # Alt_R
    "win": 0xFFEB,  # Super_L
    "winleft": 0xFFEB,  # Super_L
    "winright": 0xFFEC,  # Super_R
    "capslock": 0xFFE5,  # Caps_Lock
    "numlock": 0xFF7F,  # Num_Lock
    "scrolllock": 0xFF14,  # Scroll_Lock
    "printscreen": 0xFF61,  # Print
    "pause": 0xFF13,  # Pause
    "menu": 0xFF67,  # Menu
    "volumedown": 0x1008FF11,  # XF86AudioLowerVolume
    "volumemute": 0x1008FF12,  # XF86AudioMute
    "volumeup": 0x1008FF13,  # XF86AudioRaiseVolume
    # F1..F24 are consecutive keysyms starting at F1 (0xFFBE).
    **{f"f{n}": _KEYSYM_F1 + n - 1 for n in range(1, 25)},
}

# Control characters a lone key name may carry; everything else must be
# printable.
_CONTROL_CHAR_KEYSYMS: Final[dict[str, int]] = {
    "\n": _KEYSYM_RETURN,
    "\r": _KEYSYM_RETURN,
    "\t": _KEYSYM_TAB,
}


def resolve_key_name(name: str) -> str | None:
    """Return the canonical name for ``name``, or None if it is not a known key.

    A single character keeps its case (pyautogui treats a lone uppercase
    letter as shift+letter); longer names are matched case-insensitively.
    """
    if len(name) == 1:
        if name in _CONTROL_CHAR_KEYSYMS or name.isprintable():
            return name
        return None
    lowered = name.lower()
    canonical = _KEY_ALIASES.get(lowered, lowered)
    return canonical if canonical in _NAMED_KEYSYMS else None


def key_name_to_keysym(name: str) -> int:
    """Return the keysym for a canonical name from ``resolve_key_name``.

    A single character goes through ``char_to_keysym``; for an uppercase
    letter that yields the uppercase keysym, and Mutter adds Shift itself.
    Raises ``ValueError`` for a name ``resolve_key_name`` would not return.
    """
    if len(name) == 1:
        return char_to_keysym(name)
    keysym = _NAMED_KEYSYMS.get(name)
    if keysym is None:
        raise ValueError(f"Unknown key name: {name!r}")
    return keysym


def char_to_keysym(char: str) -> int:
    """Return the keysym that types ``char``.

    Latin-1 printable code points are their own keysyms; newline and tab map
    to Return and Tab; any other character uses the Unicode keysym range.
    """
    if len(char) != 1:
        raise ValueError(f"Expected exactly one character, got {char!r}")
    control = _CONTROL_CHAR_KEYSYMS.get(char)
    if control is not None:
        return control
    code_point = ord(char)
    if 0x20 <= code_point <= 0x7E or 0xA0 <= code_point <= 0xFF:
        return code_point
    return _UNICODE_KEYSYM_OFFSET + code_point
