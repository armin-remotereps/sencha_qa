"""TestRail case text (HTML or Markdown) as readable plain text, for display.

TestRail fields are Markdown, or HTML once the instance uses the rich-text
editor. People should read the text, not its tags: the Test Runner window and
the web case pages show ``markup_to_plain`` of the raw field. Editing, agent
prompts and the TestRail push keep the raw text.

Pure stdlib, no Django. The output is plain text: templates autoescape it.
"""

from __future__ import annotations

import re
from functools import lru_cache
from html.parser import HTMLParser
from typing import Final

# Bounds the work on a huge field; every caller caps its output further.
_MAX_INPUT_CHARS: Final = 64_000

# HTML only when a known element is closed (``<p>..</p>``) or a void tag
# appears, so "a < b", "<username>" or "check the <div> wrapper" stay text.
_HTML_CLOSING_TAG: Final = re.compile(
    r"</(?:p|div|span|ul|ol|li|strong|b|em|i|u|s|a|h[1-6]|table|thead|tbody"
    r"|tr|td|th|pre|code|blockquote)[ \t]*>",
    re.IGNORECASE,
)
_HTML_VOID_TAG: Final = re.compile(
    r"<(?:br|hr)[ \t]*/?>|<img\b[^<>]{0,1000}>", re.IGNORECASE
)
# Longer lines skip the inline Markdown passes (bounded work per field).
_MAX_INLINE_LINE_CHARS: Final = 4_000

_BLOCK_TAGS: Final = frozenset(
    {
        "p",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "pre",
        "table",
        "thead",
        "tbody",
        "tr",
        "ul",
        "ol",
        "hr",
    }
)
_DROPPED_TAGS: Final = frozenset({"script", "style", "head", "title"})
_SPACES: Final = re.compile(r"[ \t\r\n\f\v ]+")
_BLANK_LINES: Final = re.compile(r"\n{3,}")

# Markdown, line based; ``[ \t]`` instead of ``\s`` and no nested quantifiers.
_FENCE: Final = re.compile(r"^[ \t]*(?:```|~~~)")
_HEADING: Final = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+")
_BULLET: Final = re.compile(r"^([ \t]*)[*+-][ \t]+")
_QUOTE: Final = re.compile(r"^[ \t]{0,3}>[ \t]?")
_TABLE_ROW: Final = re.compile(r"^[ \t]*\|\|\|?")
_IMAGE: Final = re.compile(r"!\[([^\]\n]{0,500})\]\(([^)\s]{0,500})\)")
_LINK: Final = re.compile(r"\[([^\]\n]{1,500})\]\(([^)\s]{1,500})\)")
_BOLD: Final = re.compile(r"(?<!\\)(\*\*|__)(?=\S)([^\n]{1,500}?)(?<=[^\s\\])\1")
# Single stars only between non-word boundaries, so "2 * 3 * 4" stays.
_ITALIC: Final = re.compile(
    r"(?<![\w*\\])\*(?=[^\s*])([^*\n]{1,500}?)(?<=[^\s*\\])\*(?![\w*])"
)
_CODE: Final = re.compile(r"`([^`\n]{1,500})`")
_ESCAPE: Final = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>])")


@lru_cache(maxsize=512)
def markup_to_plain(text: str) -> str:
    """``text`` without HTML tags or Markdown markers; plain text stays.

    Cached: runner snapshots convert the same case texts on every push.
    """
    text = text[:_MAX_INPUT_CHARS].replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return ""
    if _HTML_CLOSING_TAG.search(text) or _HTML_VOID_TAG.search(text):
        try:
            plain = _html_to_plain(text)
        except Exception:  # noqa: BLE001 - never fail a page on odd markup
            return text.strip()
    else:
        plain = _markdown_to_plain(text)
    lines = [line.rstrip() for line in plain.split("\n")]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip("\n")


def _markdown_to_plain(text: str) -> str:
    out: list[str] = []
    for line in text.split("\n"):
        if _FENCE.match(line):
            continue
        line = _HEADING.sub("", line)
        line = _QUOTE.sub("", line)
        if _TABLE_ROW.match(line):
            # TestRail tables: "|||:Header|Header" and "|| cell | cell".
            cells = [cell.strip().lstrip(":") for cell in line.split("|")]
            line = " | ".join(cell for cell in cells if cell)
        line = _BULLET.sub(r"\1• ", line)
        if len(line) > _MAX_INLINE_LINE_CHARS:
            out.append(line)
            continue
        line = _IMAGE.sub(_image_text, line)
        line = _LINK.sub(_link_text, line)
        line = _BOLD.sub(r"\2", line)
        line = _ITALIC.sub(r"\1", line)
        line = _CODE.sub(r"\1", line)
        out.append(_ESCAPE.sub(r"\1", line))
    return "\n".join(out)


def _image_text(match: re.Match[str]) -> str:
    alt = match.group(1).strip()
    return f"[image: {alt}]" if alt else "[image]"


def _link_text(match: re.Match[str]) -> str:
    label, url = match.group(1).strip(), match.group(2)
    return label if label == url else f"{label} ({url})"


def _html_to_plain(text: str) -> str:
    parser = _PlainTextParser()
    parser.feed(text)
    parser.close()
    return parser.text()


class _PlainTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        # One entry per open list: None for <ul>, the next number for <ol>.
        self._lists: list[int | None] = []
        self._dropped = 0
        self._pre = 0
        self._cells_in_row = 0
        self._href: str | None = None
        self._link_start = 0
        # A list marker was written and its item has no text yet.
        self._marker_open = False

    def text(self) -> str:
        return "".join(self._parts)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROPPED_TAGS:
            self._dropped += 1
            return
        if tag == "br":
            self._parts.append("\n")
        elif tag in ("ul", "ol"):
            self._newline()
            self._lists.append(1 if tag == "ol" else None)
        elif tag == "li":
            self._marker_open = False
            self._newline()
            self._parts.append("  " * max(len(self._lists) - 1, 0))
            if self._lists and self._lists[-1] is not None:
                number = self._lists[-1]
                self._lists[-1] = number + 1
                self._parts.append(f"{number}. ")
            else:
                self._parts.append("• ")
            self._marker_open = True
        elif tag == "tr":
            self._newline()
            self._cells_in_row = 0
        elif tag in ("td", "th"):
            if self._cells_in_row:
                self._parts.append(" | ")
            self._cells_in_row += 1
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self._href = href if href.startswith(("http://", "https://")) else None
            self._link_start = len(self._parts)
        elif tag == "img":
            alt = (dict(attrs).get("alt") or "").strip()
            self._parts.append(f"[image: {alt}]" if alt else "[image]")
        elif tag in _BLOCK_TAGS:
            self._newline()
            if tag == "pre":
                self._pre += 1

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in ("br", "img", "hr"):
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROPPED_TAGS:
            self._dropped = max(self._dropped - 1, 0)
            return
        if tag in ("ul", "ol"):
            if self._lists:
                self._lists.pop()
            self._newline()
        elif tag == "a":
            if self._href is not None:
                label = "".join(self._parts[self._link_start :]).strip()
                if label != self._href:
                    self._parts.append(f" ({self._href})")
            self._href = None
        elif tag in _BLOCK_TAGS or tag == "li":
            if tag == "li":
                self._marker_open = False
            if tag == "pre":
                self._pre = max(self._pre - 1, 0)
            self._newline()

    def handle_data(self, data: str) -> None:
        if self._dropped:
            return
        if self._pre:
            self._parts.append(data.replace(" ", " "))
            return
        collapsed = _SPACES.sub(" ", data)
        if self._at_line_start():
            collapsed = collapsed.lstrip(" ")
        if collapsed:
            self._parts.append(collapsed)
            self._marker_open = False

    def _at_line_start(self) -> bool:
        for part in reversed(self._parts):
            if part:
                return part.endswith(("\n", " "))
        return True

    def _newline(self) -> None:
        if self._marker_open:
            # "<li><p>text</p></li>": keep the text on the marker's line.
            return
        # Drop the trailing spaces of the line, then end it once.
        while self._parts and self._parts[-1].strip(" ") == "":
            if "\n" in self._parts[-1]:
                break
            self._parts.pop()
        if self._parts:
            self._parts[-1] = self._parts[-1].rstrip(" ")
            if not self._parts[-1].endswith("\n"):
                self._parts.append("\n")
