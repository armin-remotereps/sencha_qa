"""The chat with the agent, shared by the person window's tabs.

The Discussion tab (a Need help case's thread) and the Manual run tab (the
claimed case's discussion, MANUAL_PAUSE_DISCUSS_V1) both show the newest rows
of a setup thread, "The agent is working…" while a turn is live, and a message
box with Send, the server's reason when a message cannot be sent and the
outcome of the last send. Runs on the window's Tk thread; every text is plain
Tk text. The rows are rendered once per change: new rows of the same thread
are appended, anything else re-renders the whole chat.
"""

from collections.abc import Callable, Hashable
from datetime import datetime
from typing import Final, Protocol

from controller_client.protocol import (
    DiscussionMessageKind,
    PersonDiscussionMessagePayload,
)

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:  # the window process reports a missing tkinter itself
    pass

# Same palette as person_window_app.
_WARN_COLOR: Final[str] = "#b45309"
_MUTED_COLOR: Final[str] = "#6b7280"
_BOLD_FONT: Final[str] = "PunkHazardBold"
_EMPTY: Final[str] = "No messages yet. Write the first one below."

_Segments = tuple[tuple[str, tuple[str, ...]], ...]


class ChatBlock(Protocol):
    """The read-only block the rows go into (``_ReadOnlyBlock``)."""

    frame: "ttk.Frame"
    text: "tk.Text"


class ChatHost(Protocol):
    """The widgets the chat needs from the window (``PersonWindowApp``)."""

    def chat_block(
        self, parent: "ttk.Frame", *, title: str, fixed_lines: int
    ) -> ChatBlock: ...

    def chat_label(
        self, parent: "tk.Misc", text: str = "", foreground: str = ""
    ) -> "ttk.Label": ...

    def chat_entry(self, parent: "tk.Misc", *, row: int, height: int) -> "tk.Text": ...


def clock(created_at: str) -> str:
    """ "14:05" in local time for an ISO 8601 time; "" when unreadable."""
    try:
        return datetime.fromisoformat(created_at).astimezone().strftime("%H:%M")
    except ValueError:
        return ""


def chat_segments(message: PersonDiscussionMessagePayload, first: bool) -> _Segments:
    gap = "" if first else "\n\n"
    if message.kind is DiscussionMessageKind.NOTICE:
        return ((f"{gap}{message.text}", ("notice",)),)
    author = message.author or (
        "Agent" if message.kind is DiscussionMessageKind.ASSISTANT else "Someone"
    )
    at = clock(message.created_at)
    head = f"{author} · {at}" if at else author
    return ((f"{gap}{head}\n", ("author",)), (message.text, ("body",)))


class ChatPanel:
    """The chat, the "working" line and the message box, gridded into
    ``parent`` at ``row`` (chat), ``row + 1`` (working) and ``row + 2``
    (message box)."""

    def __init__(
        self,
        host: ChatHost,
        parent: "ttk.Frame",
        *,
        row: int,
        title: str,
        lines: int,
        on_send: Callable[[], None],
        on_input: Callable[[], None],
        working_text: str = "The agent is working…",
    ) -> None:
        self.block = host.chat_block(parent, title=title, fixed_lines=lines)
        self.block.text.tag_configure("author", font=_BOLD_FONT)
        self.block.text.tag_configure("notice", foreground=_MUTED_COLOR)
        self.block.frame.grid(row=row, column=0, sticky="ew", pady=(6, 0))
        self._working = host.chat_label(parent, working_text, foreground=_MUTED_COLOR)
        self._working.grid(row=row + 1, column=0, sticky="w")

        send = ttk.Frame(parent)
        send.columnconfigure(0, weight=1)
        send.grid(row=row + 2, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(send, text="Your message").grid(row=0, column=0, sticky="w")
        self.input = host.chat_entry(send, row=1, height=3)
        self.input.bind("<<Modified>>", self._on_modified)
        send_row = ttk.Frame(send)
        send_row.grid(row=2, column=0, sticky="w", pady=(4, 0))
        self._send_button = ttk.Button(send_row, text="Send", command=on_send)
        self._send_button.pack(side="left")
        self._send_blocker = host.chat_label(send, "", foreground=_WARN_COLOR)
        self._send_blocker.grid(row=3, column=0, sticky="w")
        self._status = host.chat_label(send, "")
        self._status.grid(row=4, column=0, sticky="w")
        self._on_input = on_input
        # What the chat shows: the thread it belongs to and its rows.
        self._thread: Hashable = None
        self._rows: tuple[PersonDiscussionMessagePayload, ...] = ()

    # --- rows -----------------------------------------------------------------

    def render_rows(
        self, thread: Hashable, rows: tuple[PersonDiscussionMessagePayload, ...]
    ) -> None:
        """Show ``rows`` of ``thread``; appends when only new rows came."""
        text = self.block.text
        old = self._rows
        if (
            self._thread == thread
            and old
            and len(rows) >= len(old)
            and rows[: len(old)] == old
        ):
            new = rows[len(old) :]
            if not new:
                return
            at_end = text.yview()[1] >= 0.999
            for message in new:
                for chunk, tags in chat_segments(message, first=not self._rows):
                    text.insert("end", chunk, tags)
                self._rows += (message,)
        else:
            # Another thread, an earlier row changed or rows were dropped.
            at_end = True
            text.delete("1.0", "end")
            for index, message in enumerate(rows):
                for chunk, tags in chat_segments(message, first=index == 0):
                    text.insert("end", chunk, tags)
            if not rows:
                text.insert("end", _EMPTY, ("notice",))
            self._rows = rows
            self._thread = thread
        if at_end:
            text.see("end")

    def set_working(self, live: bool) -> None:
        if live:
            self._working.grid()
        else:
            self._working.grid_remove()

    # --- message box ------------------------------------------------------------

    def input_text(self) -> str:
        return self.input.get("1.0", "end-1c")

    def set_input(self, text: str) -> None:
        self.input.delete("1.0", "end")
        self.input.insert("1.0", text)

    def render_send(
        self, *, connected: bool, can_send: bool, busy: bool, send_blocker: str
    ) -> None:
        can = connected and can_send and not busy and bool(self.input_text().strip())
        self._send_button.configure(
            text="Sending…" if busy else "Send",
            state="normal" if can else "disabled",
        )
        self._send_blocker.configure(text="" if can_send else send_blocker)

    def set_status(self, text: str, foreground: str = "") -> None:
        self._status.configure(text=text, foreground=foreground)

    def _on_modified(self, _event: "tk.Event[tk.Text]") -> None:
        self.input.edit_modified(False)
        self._on_input()
