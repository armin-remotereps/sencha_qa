"""Clean a shell command's captured output before the agent reads it.

Pure: no ORM, no settings and no ``agents.services`` import (that package's
``__init__`` imports ``projects.services``; see ``agents/checkpoint.py``).

Runners capture raw terminal output: colour codes, cursor movement, progress
bars redrawn with carriage returns and long runs of identical lines (repeated
stack frames, retry loops). Those cost tokens and hide the useful text, so the
output is reduced to what a terminal would finally show.
"""

from __future__ import annotations

import re
from typing import Final

# CSI: ESC [ parameters/intermediates final byte. OSC: ESC ] ... (BEL | ESC \).
# Any other escape: ESC plus one character.
_ANSI_PATTERN: Final = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]" r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)" r"|\x1b[^\n]?"
)
# C0 controls (and DEL) except \t, \n, \r (handled later) and \b (handled below).
_CONTROL_PATTERN: Final = re.compile(r"[\x00-\x07\x0b\x0c\x0e-\x1f\x7f]")
_MIN_REPEAT_RUN: Final = 3


def clean_command_output(text: str) -> str:
    """Return ``text`` without terminal noise, as a terminal would show it."""
    text = text.replace("\r\n", "\n")
    text = _ANSI_PATTERN.sub("", text)
    text = _CONTROL_PATTERN.sub("", text)
    if "\b" in text:
        text = _apply_backspaces(text)
    lines = [_last_redraw(line) for line in text.split("\n")]
    # Trailing blank lines carry nothing; keep one so a final newline survives.
    while len(lines) > 2 and lines[-1] == "" and lines[-2] == "":
        lines.pop()
    return "\n".join(_collapse_repeats(lines))


def _apply_backspaces(text: str) -> str:
    kept: list[str] = []
    for char in text:
        if char != "\b":
            kept.append(char)
        elif kept and kept[-1] != "\n":
            kept.pop()
    return "".join(kept)


def _last_redraw(line: str) -> str:
    # A trailing \r only moves the cursor; the text before it stays on screen.
    line = line.rstrip("\r")
    return line.rsplit("\r", 1)[-1]


def _collapse_repeats(lines: list[str]) -> list[str]:
    result: list[str] = []
    index = 0
    while index < len(lines):
        end = index + 1
        while end < len(lines) and lines[end] == lines[index]:
            end += 1
        run = end - index
        result.append(lines[index])
        if run >= _MIN_REPEAT_RUN:
            result.append(f"[previous line repeated {run - 1} more times]")
        elif run == 2:
            result.append(lines[index])
        index = end
    return result
