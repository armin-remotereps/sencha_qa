"""The "How to do it" block of the person window, shared by its tabs.

The Human in the loop Step tab and the Manual run tab both show the AI's
numbered how-to items for a step: each item's text, its "Source: …" line,
and a suggested command with a note and one Copy button per command. Runs on
the window's Tk thread; every text is plain Tk text.
"""

import functools
from collections.abc import Callable
from typing import Final, Protocol

from controller_client.protocol import PersonHowToPayload

try:
    from tkinter import ttk
except ImportError:  # the window process reports a missing tkinter itself
    pass

SUGGESTED_NOTE: Final[str] = "Suggested by the AI. Check it before you run it."

_Segments = tuple[tuple[str, tuple[str, ...]], ...]


class HowToBlock(Protocol):
    """The read-only block the items go into (``_ReadOnlyBlock``)."""

    title: "ttk.Label"

    def set_segments(self, segments: _Segments) -> bool: ...

    def show(self, visible: bool) -> None: ...


def how_to_segments(items: tuple[PersonHowToPayload, ...]) -> _Segments:
    """The items as text segments (tags ``muted`` and ``mono``)."""
    segments: list[tuple[str, tuple[str, ...]]] = []
    for number, item in enumerate(items, start=1):
        prefix = "" if number == 1 else "\n"
        segments.append((f"{prefix}{number}. {item.text}", ()))
        if item.source:
            source = " ".join(item.source.splitlines())
            segments.append((f"\n    Source: {source}", ("muted",)))
        if not item.command:
            continue
        command = " ".join(item.command.splitlines())
        segments.append((f"\n    {command}", ("mono",)))
        segments.append((f"\n    {SUGGESTED_NOTE}", ("muted",)))
    return tuple(segments)


def clear_copy_buttons(copies: "ttk.Frame") -> None:
    for child in copies.winfo_children():
        child.destroy()


def render_how_to(
    block: HowToBlock,
    copies: "ttk.Frame",
    items: tuple[PersonHowToPayload, ...],
    *,
    title: str,
    copy: Callable[[str], None],
) -> None:
    """Fill the block and rebuild its Copy buttons; hidden without items."""
    clear_copy_buttons(copies)
    if not items:
        block.show(False)
        return
    block.title.configure(text=title)
    for number, item in enumerate(items, start=1):
        if item.command:
            ttk.Button(
                copies,
                text=f"Copy command {number}",
                command=functools.partial(copy, item.command),
            ).pack(side="left", padx=(0, 6))
    block.set_segments(how_to_segments(items))
    block.show(True)
