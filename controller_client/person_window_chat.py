"""The chat with the agent, shared by the person window's tabs.

The Discussion tab (a Need help case's thread) and the Manual run tab (the
claimed case's discussion, MANUAL_PAUSE_DISCUSS_V1) both show the newest rows
of a setup thread, "The agent is working…" while a turn is live, and a message
box with Send, the server's reason when a message cannot be sent and the
outcome of the last send. Runs on the window's Tk thread. The rows are
rendered once per change: new rows of the same thread are appended, anything
else re-renders the whole chat.

Messages are bubbles inside the one read-only text (selection and Copy work
across messages): the agent's on the left, every person's on the right,
notices centred and muted without a bubble. Bodies are a Markdown subset
(``chat_markdown``) mapped to text tags; a link is clickable only when its
URL is plain-ASCII http/https without invisible characters, and a click opens
it in the default browser. A clickable link whose label is not its URL shows
the host after it in muted text; any other link shows its target the same way,
as plain text that can be copied.
"""

import unicodedata
from collections.abc import Callable, Hashable
from datetime import datetime
from typing import Final, Protocol
from urllib.parse import urlsplit

from controller_client.chat_markdown import (
    BULLET_TEXT,
    MAX_LIST_LEVEL,
    Inline,
    LineStyle,
    MdSegment,
    render_markdown,
)
from controller_client.person_window_theme import (
    AGENT_BUBBLE_BG,
    BOLD_FONT,
    BOLD_ITALIC_FONT,
    CODE_BG,
    CODE_TEXT,
    CYAN,
    HEADING_FONT,
    INFO,
    ITALIC_FONT,
    MUTED,
    SUBHEADING_FONT,
    USER_BUBBLE_BG,
    WARN,
)
from controller_client.protocol import (
    DiscussionMessageKind,
    PersonDiscussionMessagePayload,
)

try:
    import tkinter as tk
    from tkinter import font as tkfont
    from tkinter import ttk
except ImportError:  # the window process reports a missing tkinter itself
    pass

_EMPTY: Final[str] = "No messages yet. Write the first one below."

# The bubbles' sides and their tags.
_AGENT: Final[str] = "agent"
_USER: Final[str] = "user"
_SIDES: Final[tuple[str, ...]] = (_AGENT, _USER)
_BUBBLE_COLORS: Final[dict[str, str]] = {
    _AGENT: AGENT_BUBBLE_BG,
    _USER: USER_BUBBLE_BG,
}
# The empty side of a bubble: this share of the text width.
_GUTTER_SHARE: Final[float] = 0.18
# Inner padding (pixels) on the bubble's open side, and above / below a bubble.
_BUBBLE_PAD_PX: Final[int] = 8
_BUBBLE_SPACING_PX: Final[int] = 5
# One list / quote level of indentation: this many "0" of the text font.
_INDENT_CHARS: Final[str] = "000"
# A person's bubble is indented by text (its left margin is the page colour,
# see ChatPanel.relayout): one level is these spaces.
_TEXT_INDENT: Final[str] = "    "
_LINK_TAG: Final[str] = "link"
_LINK_PREFIX: Final[str] = "link-"
_HAND_CURSOR: Final[str] = "hand2"
_MUTED_TAG: Final[str] = "md_muted"
# Characters a clickable URL must not contain: format (bidi, zero width),
# control, and line / paragraph / space separators.
_URL_REJECTED_CATEGORIES: Final[frozenset[str]] = frozenset(
    {"Cf", "Cc", "Zl", "Zp", "Zs"}
)

# Tags of a body line's style (line-level, also on its newline).
_LINE_TAGS: Final[dict[LineStyle, str]] = {
    LineStyle.HEADING1: "md_h1",
    LineStyle.HEADING2: "md_h2",
    LineStyle.HEADING3: "md_h3",
    LineStyle.QUOTE: "md_quote",
    LineStyle.PRE: "md_pre",
    LineStyle.TABLE: "md_table",
    LineStyle.RULE: "md_rule",
}
_LIST_STYLES: Final[frozenset[LineStyle]] = frozenset(
    {LineStyle.BULLET, LineStyle.NUMBERED}
)

# A run of text, its tags and (only for an http/https link) the link's URL.
_Segment = tuple[str, tuple[str, ...], str]
_Segments = tuple[_Segment, ...]


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

    def chat_open_url(self, url: str) -> None: ...


def clock(created_at: str) -> str:
    """ "14:05" in local time for an ISO 8601 time; "" when unreadable."""
    try:
        return datetime.fromisoformat(created_at).astimezone().strftime("%H:%M")
    except ValueError:
        return ""


def _web_url(url: str) -> bool:
    """Only ASCII http(s) links without blanks, control or invisible
    characters and with a host are opened (no look-alike hosts). A backslash
    (browsers read it as "/", urlsplit does not) and user info ("user@host")
    are refused too, so the host shown next to a link is the one opened."""
    return (
        url.startswith(("https://", "http://"))
        and url.isascii()
        and "\\" not in url
        and "@" not in _url_netloc(url)
        and not any(
            char.isspace() or unicodedata.category(char) in _URL_REJECTED_CATEGORIES
            for char in url
        )
        and bool(_url_host(url))
    )


def _url_netloc(url: str) -> str:
    """The URL's network location ("" when it cannot be read)."""
    try:
        return urlsplit(url).netloc
    except ValueError:
        return ""


def _url_host(url: str) -> str:
    """The URL's host ("" when it has none or cannot be read)."""
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _margin_tag(side: str, segment: MdSegment) -> str:
    """The line's margin tag: side, and for the agent the list / quote level.
    A person's bubble indents by text instead (``_text_indent``)."""
    if side == _AGENT:
        if segment.line in _LIST_STYLES:
            return f"{side}:list{min(segment.indent, MAX_LIST_LEVEL)}"
        if segment.line is LineStyle.QUOTE:
            return f"{side}:quote"
    return f"{side}:plain"


def _text_indent(side: str, segment: MdSegment) -> str:
    """Leading spaces of a line in a person's bubble (list level or quote)."""
    if side != _USER:
        return ""
    if segment.line in _LIST_STYLES:
        return _TEXT_INDENT * min(segment.indent, MAX_LIST_LEVEL)
    if segment.line is LineStyle.QUOTE:
        return _TEXT_INDENT
    return ""


def _inline_tag(inline: frozenset[Inline]) -> str:
    if Inline.CODE in inline:
        return "md_code"
    if Inline.BOLD in inline and Inline.ITALIC in inline:
        return "md_bold_italic"
    if Inline.BOLD in inline:
        return "md_bold"
    if Inline.ITALIC in inline:
        return "md_italic"
    return ""


def _line_tags(side: str, segment: MdSegment) -> tuple[str, ...]:
    """Bubble, margin and line-style tags of the line ``segment`` is on."""
    style = _LINE_TAGS.get(segment.line, "")
    base = (f"bubble_{side}", _margin_tag(side, segment))
    return (*base, style) if style else base


def _body_segments(side: str, text: str) -> list[_Segment]:
    """The bubble's body: rendered Markdown, the last line (and the bubble's
    closing newline) tagged for the bottom padding."""
    rendered = render_markdown(text)
    if not rendered:
        return []
    last_line_start = 0
    for index, segment in enumerate(rendered):
        if segment.text.endswith("\n"):
            last_line_start = index + 1
    body: list[_Segment] = []
    line_start = True
    for index, segment in enumerate(rendered):
        tags = _line_tags(side, segment)
        if index >= last_line_start:
            tags = (*tags, "bubble_tail")
        if line_start:
            indent = _text_indent(side, segment)
            if indent:
                body.append((indent, tags, ""))
        inline = _inline_tag(segment.inline)
        styled = (*tags, inline) if inline else tags
        if Inline.LINK not in segment.inline:
            body.append((segment.text, styled, ""))
        elif _web_url(segment.url):
            body.append((segment.text, styled, segment.url))
            if segment.text != segment.url:
                # Anti-spoofing: the real host, outside the link.
                host = _url_host(segment.url)
                body.append((f" ({host})", (*tags, _MUTED_TAG), ""))
        else:
            # Not openable: label and target as plain text.
            body.append((segment.text, styled, ""))
            if segment.text != segment.url:
                body.append((f" ({segment.url})", (*tags, _MUTED_TAG), ""))
        line_start = segment.text.endswith("\n")
    last = rendered[-1]
    # The closing newline carries the last line's tags, so the bubble's
    # background reaches its right edge and the bottom padding applies.
    body.append(("\n", (*_line_tags(side, last), "bubble_tail"), ""))
    return body


def chat_segments(message: PersonDiscussionMessagePayload, first: bool) -> _Segments:
    """One message as tagged segments. Every message ends with a newline;
    messages after the first start with one blank, untagged line."""
    gap: _Segments = () if first else (("\n", (), ""),)
    if message.kind is DiscussionMessageKind.NOTICE:
        return (*gap, (f"{message.text}\n", ("notice",), ""))
    agent = message.kind is DiscussionMessageKind.ASSISTANT
    side = _AGENT if agent else _USER
    author = message.author or ("Agent" if agent else "Someone")
    at = clock(message.created_at)
    head = f"{author} · {at}" if at else author
    body = _body_segments(side, message.text)
    head_tags: tuple[str, ...] = (
        f"bubble_{side}",
        f"{side}:plain",
        f"author_{side}",
        "bubble_head",
    )
    if not body:
        head_tags = (*head_tags, "bubble_tail")
    return (*gap, (f"{head}\n", head_tags, ""), *body)


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
        self._host = host
        self.block = host.chat_block(parent, title=title, fixed_lines=lines)
        self._configure_tags()
        self.block.text.bind("<Configure>", self._on_configure, add="+")
        self.block.frame.grid(row=row, column=0, sticky="ew", pady=(6, 0))
        self._working = host.chat_label(parent, working_text, foreground=MUTED)
        self._working.grid(row=row + 1, column=0, sticky="w")

        send = ttk.Frame(parent)
        send.columnconfigure(0, weight=1)
        send.grid(row=row + 2, column=0, sticky="ew", pady=(6, 0))
        # The message box (a section of the page it is on).
        self.message_frame = send
        ttk.Label(send, text="Your message").grid(row=0, column=0, sticky="w")
        self.input = host.chat_entry(send, row=1, height=3)
        self.input.bind("<<Modified>>", self._on_modified)
        send_row = ttk.Frame(send)
        send_row.grid(row=2, column=0, sticky="w", pady=(4, 0))
        self._send_button = ttk.Button(send_row, text="Send", command=on_send)
        self._send_button.pack(side="left")
        self._send_blocker = host.chat_label(send, "", foreground=WARN)
        self._send_blocker.grid(row=3, column=0, sticky="w")
        self._status = host.chat_label(send, "")
        self._status.grid(row=4, column=0, sticky="w")
        self._on_input = on_input
        # What the chat shows: the thread it belongs to and its rows.
        self._thread: Hashable = None
        self._rows: tuple[PersonDiscussionMessagePayload, ...] = ()
        # Clickable links: one tag per link ("link-N") and its URL.
        self._links: dict[str, str] = {}
        self._link_count = 0
        self._pressed_link = ""
        # (inner width, indent step, list hang) the margins were set for.
        self._layout_key: tuple[int, int, int] | None = None

    # --- tags -----------------------------------------------------------------

    def _configure_tags(self) -> None:
        """Create the tags in priority order (later wins): bubbles, margins,
        padding, authors, line styles, inline styles, links, then code so its
        background wins over the bubble's."""
        text = self.block.text
        text.tag_configure("notice", foreground=MUTED, justify="center")
        page = text.cget("background")
        for side in _SIDES:
            text.tag_configure(f"bubble_{side}", background=_BUBBLE_COLORS[side])
        # Margins: the agent's open (left) margin and a person's open (right)
        # margin are the bubble's inner padding; the gutter side is the page.
        # Tk paints a margin in the tag's background unless lmargincolor /
        # rmargincolor say otherwise (Tk >= 8.6.6; older Tk: full-width
        # bubbles).
        margin_colors = {
            _AGENT: {"lmargincolor": AGENT_BUBBLE_BG, "rmargincolor": page},
            _USER: {"lmargincolor": page, "rmargincolor": USER_BUBBLE_BG},
        }
        for side in _SIDES:
            for key in self._margin_keys(side):
                name = f"{side}:{key}"
                text.tag_configure(name)
                try:
                    text.tag_configure(name, **margin_colors[side])
                except tk.TclError:
                    pass
        text.tag_configure("bubble_head", spacing1=_BUBBLE_SPACING_PX)
        text.tag_configure("bubble_tail", spacing3=_BUBBLE_SPACING_PX)
        text.tag_configure(f"author_{_AGENT}", font=BOLD_FONT, foreground=MUTED)
        text.tag_configure(f"author_{_USER}", font=BOLD_FONT, foreground=CYAN)
        text.tag_configure("md_quote", foreground=MUTED)
        text.tag_configure("md_rule", foreground=MUTED)
        text.tag_configure(_MUTED_TAG, foreground=MUTED)
        text.tag_configure("md_italic", font=ITALIC_FONT)
        text.tag_configure("md_bold", font=BOLD_FONT)
        text.tag_configure("md_bold_italic", font=BOLD_ITALIC_FONT)
        text.tag_configure("md_h3", font=BOLD_FONT)
        text.tag_configure("md_h2", font=SUBHEADING_FONT)
        text.tag_configure("md_h1", font=HEADING_FONT)
        text.tag_configure("md_table", font="TkFixedFont")
        text.tag_configure(_LINK_TAG, foreground=INFO, underline=True)
        text.tag_bind(_LINK_TAG, "<Enter>", self._on_link_enter)
        text.tag_bind(_LINK_TAG, "<Leave>", self._on_link_leave)
        text.tag_bind(_LINK_TAG, "<Button-1>", self._on_link_press)
        text.tag_bind(_LINK_TAG, "<ButtonRelease-1>", self._on_link_release)
        for name in ("md_code", "md_pre"):
            text.tag_configure(
                name, font="TkFixedFont", background=CODE_BG, foreground=CODE_TEXT
            )
        # The selection stays visible over bubbles and code.
        text.tag_raise("sel")
        self._text_cursor = str(text.cget("cursor"))

    @staticmethod
    def _margin_keys(side: str) -> tuple[str, ...]:
        if side == _USER:
            return ("plain",)
        lists = tuple(f"list{level}" for level in range(MAX_LIST_LEVEL + 1))
        return ("plain", "quote", *lists)

    def relayout(self) -> None:
        """Set the margin tags' pixels from the text's width and font (no
        re-insert)."""
        text = self.block.text
        width = text.winfo_width()
        if width <= 1:
            return
        inset = 2 * (
            int(text.cget("padx"))
            + int(text.cget("highlightthickness"))
            + int(text.cget("borderwidth"))
        )
        inner = max(1, width - inset)
        font = tkfont.nametofont("TkTextFont", root=text)
        step = font.measure(_INDENT_CHARS)
        hang = max(font.measure(BULLET_TEXT), font.measure("0. "))
        key = (inner, step, hang)
        if key == self._layout_key:
            return
        self._layout_key = key
        gutter = max(_BUBBLE_PAD_PX, int(inner * _GUTTER_SHARE))
        pad = _BUBBLE_PAD_PX
        text.tag_configure(
            f"{_USER}:plain", lmargin1=gutter, lmargin2=gutter, rmargin=pad
        )
        text.tag_configure(
            f"{_AGENT}:plain", lmargin1=pad, lmargin2=pad, rmargin=gutter
        )
        text.tag_configure(
            f"{_AGENT}:quote", lmargin1=pad + step, lmargin2=pad + step, rmargin=gutter
        )
        for level in range(MAX_LIST_LEVEL + 1):
            first = pad + level * step
            text.tag_configure(
                f"{_AGENT}:list{level}",
                lmargin1=first,
                lmargin2=first + hang,
                rmargin=gutter,
            )

    def _on_configure(self, _event: "tk.Event[tk.Text]") -> None:
        # Width changes, and A−/A+ (the fixed-height text changes height).
        self.relayout()

    # --- links ----------------------------------------------------------------

    def _link_at(self, event: "tk.Event[tk.Text]") -> str:
        """The URL tag ("link-N") under the pointer; "" when none."""
        index = self.block.text.index(f"@{event.x},{event.y}")
        for name in self.block.text.tag_names(index):
            if name in self._links:
                return str(name)
        return ""

    def _on_link_enter(self, _event: "tk.Event[tk.Text]") -> None:
        self.block.text.configure(cursor=_HAND_CURSOR)

    def _on_link_leave(self, _event: "tk.Event[tk.Text]") -> None:
        self.block.text.configure(cursor=self._text_cursor)

    def _on_link_press(self, event: "tk.Event[tk.Text]") -> None:
        self._pressed_link = self._link_at(event)

    def _on_link_release(self, event: "tk.Event[tk.Text]") -> None:
        # Opens on a click that starts and ends on the same link and selects
        # nothing (a drag across a link selects, as anywhere else).
        pressed, self._pressed_link = self._pressed_link, ""
        name = self._link_at(event)
        if not name or name != pressed or self.block.text.tag_ranges("sel"):
            return
        url = self._links[name]
        if _web_url(url):
            self._host.chat_open_url(url)

    def _insert(self, segments: _Segments) -> None:
        text = self.block.text
        for chunk, tags, url in segments:
            if url:
                self._link_count += 1
                name = f"{_LINK_PREFIX}{self._link_count}"
                self._links[name] = url
                tags = (*tags, _LINK_TAG, name)
            text.insert("end", chunk, tags)

    def _clear(self) -> None:
        text = self.block.text
        text.delete("1.0", "end")
        for name in self._links:
            text.tag_delete(name)
        self._links.clear()
        self._pressed_link = ""

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
                self._insert(chat_segments(message, first=not self._rows))
                self._rows += (message,)
        else:
            # Another thread, an earlier row changed or rows were dropped.
            at_end = True
            self._clear()
            for index, message in enumerate(rows):
                self._insert(chat_segments(message, first=index == 0))
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
