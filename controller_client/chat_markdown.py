"""A small Markdown subset for the person window's chat bubbles.

Pure functions, stdlib only, no Tk: ``render_markdown`` turns a chat message into
segments from a closed vocabulary (``Inline`` and ``LineStyle``) that the chat
panel maps to its own text tags. Nothing is loaded or executed; links are only
reported (``MdSegment.url``), the caller decides whether one may be opened.

Supported (everything else stays plain text, characters kept):

* ``#``, ``##``, ``###`` headings (a space after the hashes is required);
* ``**bold**``, ``__bold__``, ``*italic*``, ``_italic_``, ``***both***``; the
  markers count only at word boundaries, so ``snake_case``, paths and ``2*3*4``
  stay as typed; ``\\*`` and other backslash escapes; unmatched markers are kept;
* ```` `code` ```` spans (nothing parsed inside), fenced ```` ``` ```` / ``~~~``
  blocks (fence lines dropped, an unterminated fence runs to the end, lines kept
  verbatim), lines starting with ``|`` (tables) kept verbatim;
* ``-``, ``*``, ``+`` bullets (shown as "• "), ``1.`` / ``1)`` numbered items
  (marker kept), up to three nesting levels by leading spaces;
* ``>`` quotes, ``---`` / ``***`` / ``___`` rules;
* ``[text](url)`` links and bare ``http://`` / ``https://`` URLs.

Every scan is linear in the input: no regular expressions, no backtracking.
"""

from __future__ import annotations

import enum
from collections import deque
from dataclasses import dataclass
from typing import Final


class Inline(enum.Enum):
    BOLD = "bold"
    ITALIC = "italic"
    CODE = "code"
    LINK = "link"


class LineStyle(enum.Enum):
    PLAIN = "plain"
    HEADING1 = "heading1"
    HEADING2 = "heading2"
    HEADING3 = "heading3"
    BULLET = "bullet"
    NUMBERED = "numbered"
    QUOTE = "quote"
    PRE = "pre"
    TABLE = "table"
    RULE = "rule"


@dataclass(frozen=True)
class MdSegment:
    """One run of rendered text inside a single line.

    ``indent`` is the list nesting level (0..3; 0 for a top-level item and for
    every line that is not a list item). ``url`` is set only on ``Inline.LINK``
    segments: the target as written.
    """

    text: str
    inline: frozenset[Inline]
    line: LineStyle
    indent: int
    url: str = ""


RULE_TEXT: Final[str] = "─" * 24
BULLET_TEXT: Final[str] = "• "
MAX_LIST_LEVEL: Final[int] = 3

_NO_STYLE: frozenset[Inline] = frozenset()
_ESCAPABLE = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
_URL_STOP = frozenset(' \t<>"`')
_URL_TRAILING = frozenset(".,;:!?'")
_URL_SCHEMES = ("http://", "https://")
_JOINERS = frozenset("./\\-")
_HEADINGS = {1: LineStyle.HEADING1, 2: LineStyle.HEADING2, 3: LineStyle.HEADING3}
_MAX_ORDINAL_DIGITS = 9

# (text, inline styles, url) before the line style is attached.
_Part = tuple[str, frozenset[Inline], str]


@dataclass
class _Token:
    """Inline token: text (plain, code or link) or a ``*`` / ``_`` run."""

    text: str = ""
    code: bool = False
    url: str = ""
    delim: str = ""
    count: int = 0


class _NextFinder:
    """First index of ``char`` at or after ``start`` (``len`` when none).

    Queries must come with non-decreasing ``start``; the last hit is cached so
    every character is scanned at most once over all queries.
    """

    def __init__(self, text: str, char: str) -> None:
        self._text = text
        self._char = char
        self._pos = -1

    def at(self, start: int) -> int:
        if self._pos < start:
            found = self._text.find(self._char, start)
            self._pos = len(self._text) if found < 0 else found
        return self._pos


def _backtick_runs(text: str) -> dict[int, deque[int]]:
    """Start positions of backtick runs, by run length, in order."""
    runs: dict[int, deque[int]] = {}
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char == "\\" and i + 1 < n and text[i + 1] in _ESCAPABLE:
            i += 2
        elif char == "`":
            j = i
            while j < n and text[j] == "`":
                j += 1
            runs.setdefault(j - i, deque()).append(i)
            i = j
        else:
            i += 1
    return runs


def _bare_url_end(text: str, start: int) -> int:
    """End (exclusive) of a bare URL starting at ``start``, trailing punctuation
    and unbalanced closing parentheses excluded."""
    n = len(text)
    end = start
    opened = 0
    closed = 0
    while end < n and text[end] not in _URL_STOP:
        if text[end] == "(":
            opened += 1
        elif text[end] == ")":
            closed += 1
        end += 1
    while end > start:
        last = text[end - 1]
        if last in _URL_TRAILING:
            end -= 1
        elif last == ")" and closed > opened:
            closed -= 1
            end -= 1
        else:
            break
    return end


def _paren_matches(text: str) -> dict[int, int]:
    """The closing ``)`` of every ``(`` that has one, matched with balanced
    parentheses inside a run without blanks (a link target ends at its
    matching ``)``, so ``(url_(with)_parens)`` keeps the whole URL)."""
    matches: dict[int, int] = {}
    open_at: list[int] = []
    for index, char in enumerate(text):
        if char == "(":
            open_at.append(index)
        elif char == ")":
            if open_at:
                matches[open_at.pop()] = index
        elif char in " \t":
            open_at.clear()
    return matches


def _word_side(text: str, index: int, step: int) -> bool:
    """Whether ``text[index]`` continues a word, path or file name: a letter or
    digit, or a joiner (``. / \\ -``) followed (in direction ``step``) by one."""
    if not 0 <= index < len(text):
        return False
    char = text[index]
    if char.isalnum():
        return True
    beyond = index + step
    return char in _JOINERS and 0 <= beyond < len(text) and text[beyond].isalnum()


def _flanking(text: str, start: int, end: int) -> tuple[bool, bool]:
    """(can open, can close) for the ``*`` / ``_`` run ``text[start:end]``: it
    opens before a non-space after a word boundary and closes after a non-space
    before one, so ``snake_case``, ``__init__.py`` and ``2*3*4`` stay as typed."""
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    can_open = (
        after != "" and not after.isspace() and not _word_side(text, start - 1, -1)
    )
    can_close = before != "" and not before.isspace() and not _word_side(text, end, 1)
    return can_open, can_close


def _close_emphasis(
    tokens: list[_Token],
    closer_index: int,
    stacks: dict[str, list[int]],
    spans: list[tuple[int, int, int]],
) -> None:
    """Match the closer at ``closer_index`` against open runs of its char."""
    closer = tokens[closer_index]
    stack = stacks[closer.delim]
    other = stacks["_" if closer.delim == "*" else "*"]
    while closer.count > 0 and stack:
        opener_index = stack[-1]
        opener = tokens[opener_index]
        used = 2 if opener.count >= 2 and closer.count >= 2 else 1
        opener.count -= used
        closer.count -= used
        while other and other[-1] > opener_index:
            other.pop()
        spans.append((opener_index, closer_index, used))
        if opener.count == 0:
            stack.pop()


def _tokenize(text: str) -> tuple[list[_Token], list[tuple[int, int, int]]]:
    """Tokens of one line plus the matched emphasis spans
    ``(opener index, closer index, 1 = italic | 2 = bold)``."""
    tokens: list[_Token] = []
    spans: list[tuple[int, int, int]] = []
    stacks: dict[str, list[int]] = {"*": [], "_": []}
    buffer: list[str] = []
    ticks = _backtick_runs(text)
    close_bracket = _NextFinder(text, "]")
    open_bracket = _NextFinder(text, "[")
    paren_matches = _paren_matches(text)

    def flush() -> None:
        if buffer:
            tokens.append(_Token(text="".join(buffer)))
            buffer.clear()

    n = len(text)
    i = 0
    while i < n:
        char = text[i]
        if char == "\\" and i + 1 < n and text[i + 1] in _ESCAPABLE:
            buffer.append(text[i + 1])
            i += 2
        elif char == "`":
            j = i
            while j < n and text[j] == "`":
                j += 1
            length = j - i
            candidates = ticks.get(length)
            while candidates and candidates[0] < j:
                candidates.popleft()
            if candidates:
                closing = candidates.popleft()
                body = text[j:closing]
                if len(body) >= 2 and body[0] == " " and body[-1] == " ":
                    if body.strip(" "):
                        body = body[1:-1]
                flush()
                tokens.append(_Token(text=body, code=True))
                i = closing + length
            else:
                buffer.append(text[i:j])
                i = j
        elif char == "[" and _link_at(text, i, close_bracket, open_bracket):
            close = close_bracket.at(i + 1)
            end = paren_matches.get(close + 1, -1)
            if end > close + 2:
                flush()
                tokens.append(
                    _Token(text=text[i + 1 : close], url=text[close + 2 : end])
                )
                i = end + 1
            else:
                buffer.append(char)
                i += 1
        elif (
            char == "h"
            and (i == 0 or not text[i - 1].isalnum())
            and text.startswith(_URL_SCHEMES, i)
        ):
            end = _bare_url_end(text, i)
            scheme_end = i + (7 if text.startswith("http://", i) else 8)
            if end > scheme_end:
                flush()
                url = text[i:end]
                tokens.append(_Token(text=url, url=url))
                i = end
            else:
                buffer.append(char)
                i += 1
        elif char in "*_":
            j = i
            while j < n and text[j] == char:
                j += 1
            can_open, can_close = _flanking(text, i, j)
            if not (can_open or can_close):
                buffer.append(text[i:j])
            else:
                flush()
                index = len(tokens)
                tokens.append(_Token(delim=char, count=j - i))
                if can_close:
                    _close_emphasis(tokens, index, stacks, spans)
                if can_open and tokens[index].count > 0:
                    stacks[char].append(index)
            i = j
        else:
            buffer.append(char)
            i += 1
    flush()
    return tokens, spans


def _link_at(
    text: str, start: int, close_bracket: _NextFinder, open_bracket: _NextFinder
) -> bool:
    """Whether ``text[start]`` opens ``[label](url)``: non-empty label without
    brackets and ``](`` right after it (the caller checks the target has a
    matching ``)`` before any blank)."""
    close = close_bracket.at(start + 1)
    n = len(text)
    return (
        close > start + 1
        and close + 2 < n
        and open_bracket.at(start + 1) > close
        and text[close + 1] == "("
        and text[close + 2] != ")"
    )


def _inline_parts(text: str) -> list[_Part]:
    """Render one line's inline Markdown into merged parts."""
    tokens, spans = _tokenize(text)
    bold_delta = [0] * (len(tokens) + 1)
    italic_delta = [0] * (len(tokens) + 1)
    for opener, closer, used in spans:
        delta = bold_delta if used == 2 else italic_delta
        delta[opener + 1] += 1
        delta[closer] -= 1

    parts: list[_Part] = []
    pieces: list[str] = []
    current: tuple[frozenset[Inline], str] = (_NO_STYLE, "")
    bold = 0
    italic = 0
    for index, token in enumerate(tokens):
        bold += bold_delta[index]
        italic += italic_delta[index]
        piece = token.delim * token.count if token.delim else token.text
        if not piece:
            continue
        styles: set[Inline] = set()
        if bold > 0:
            styles.add(Inline.BOLD)
        if italic > 0:
            styles.add(Inline.ITALIC)
        if token.code:
            styles.add(Inline.CODE)
        if token.url:
            styles.add(Inline.LINK)
        key = (frozenset(styles), token.url)
        if pieces and key != current:
            parts.append(("".join(pieces), current[0], current[1]))
            pieces = []
        current = key
        pieces.append(piece)
    if pieces:
        parts.append(("".join(pieces), current[0], current[1]))
    return parts


def _leading_width(line: str) -> tuple[int, int]:
    """(index of the first non-blank character, its column; a tab counts 4)."""
    index = 0
    width = 0
    while index < len(line) and line[index] in " \t":
        width += 4 if line[index] == "\t" else 1
        index += 1
    return index, width


def _opening_fence(line: str) -> tuple[str, int, int] | None:
    """(fence char, fence length, indent column) when ``line`` opens a fence."""
    index, width = _leading_width(line)
    rest = line[index:]
    if not rest or rest[0] not in "`~":
        return None
    char = rest[0]
    length = 0
    while length < len(rest) and rest[length] == char:
        length += 1
    if length < 3 or (char == "`" and "`" in rest[length:]):
        return None
    return char, length, width


def _is_closing_fence(line: str, char: str, length: int) -> bool:
    rest = line.strip(" \t")
    run = 0
    while run < len(rest) and rest[run] == char:
        run += 1
    return run >= length and run == len(rest)


def _strip_columns(line: str, columns: int) -> str:
    """``line`` without up to ``columns`` leading spaces."""
    index = 0
    while index < columns and index < len(line) and line[index] == " ":
        index += 1
    return line[index:]


def _is_rule(stripped: str) -> bool:
    if len(stripped) < 3 or stripped[0] not in "-*_":
        return False
    marker = stripped[0]
    count = 0
    for char in stripped:
        if char == marker:
            count += 1
        elif char not in " \t":
            return False
    return count >= 3


def _list_level(levels: list[int], width: int) -> int:
    """Nesting level of a list item at column ``width``; ``levels`` holds the
    columns of the open levels (a deeper level needs 2 or more extra spaces)."""
    while levels and levels[-1] > width:
        levels.pop()
    if not levels or width >= levels[-1] + 2:
        levels.append(width)
    return min(len(levels) - 1, MAX_LIST_LEVEL)


def _ordinal_marker(rest: str) -> str:
    """``"12."`` / ``"3)"`` when ``rest`` starts a numbered item, else ``""``."""
    digits = 0
    while (
        digits < len(rest) and digits <= _MAX_ORDINAL_DIGITS and rest[digits].isdigit()
    ):
        digits += 1
    if (
        0 < digits <= _MAX_ORDINAL_DIGITS
        and digits + 1 < len(rest)
        and rest[digits] in ".)"
        and rest[digits + 1] in " \t"
    ):
        return rest[: digits + 1]
    return ""


def _strip_quote(rest: str) -> str:
    """Quote text without its ``>`` markers (nested ``> >`` included)."""
    index = 0
    while index < len(rest) and rest[index] == ">":
        index += 1
        if index < len(rest) and rest[index] in " \t":
            index += 1
    return rest[index:]


def _render_line(line: str, levels: list[int]) -> tuple[LineStyle, int, list[_Part]]:
    """Line style, list level and parts of one line outside a fence."""
    index, width = _leading_width(line)
    rest = line[index:]
    if not rest:
        return LineStyle.PLAIN, 0, []
    if rest[0] == "|":
        levels.clear()
        return LineStyle.TABLE, 0, [(line, _NO_STYLE, "")]
    if _is_rule(rest.rstrip(" \t")):
        levels.clear()
        return LineStyle.RULE, 0, [(RULE_TEXT, _NO_STYLE, "")]
    if len(rest) >= 2 and rest[0] in "-*+" and rest[1] in " \t":
        level = _list_level(levels, width)
        parts = [(BULLET_TEXT, _NO_STYLE, "")]
        return LineStyle.BULLET, level, parts + _inline_parts(rest[2:].lstrip(" \t"))
    marker = _ordinal_marker(rest)
    if marker:
        level = _list_level(levels, width)
        parts = [(marker + " ", _NO_STYLE, "")]
        body = rest[len(marker) + 1 :].lstrip(" \t")
        return LineStyle.NUMBERED, level, parts + _inline_parts(body)
    levels.clear()
    if rest[0] == "#" and width <= 3:
        hashes = 0
        while hashes < len(rest) and rest[hashes] == "#":
            hashes += 1
        if hashes in _HEADINGS and hashes < len(rest) and rest[hashes] == " ":
            return _HEADINGS[hashes], 0, _inline_parts(rest[hashes + 1 :].strip(" \t"))
    if rest[0] == ">":
        return LineStyle.QUOTE, 0, _inline_parts(_strip_quote(rest))
    return LineStyle.PLAIN, 0, _inline_parts(line)


def _is_blank(parts: list[_Part]) -> bool:
    return all(not part[0].strip() for part in parts)


def render_markdown(text: str) -> tuple[MdSegment, ...]:
    """Render ``text`` into segments whose texts concatenate to the readable
    message. Every line but the last ends with ``"\\n"``, carried by the last
    segment of its line; trailing blank lines are dropped."""
    rendered: list[tuple[LineStyle, int, list[_Part]]] = []
    levels: list[int] = []
    fence_char = ""
    fence_length = 0
    fence_indent = 0
    for raw in text.split("\n"):
        line = raw[:-1] if raw.endswith("\r") else raw
        if fence_char:
            if _is_closing_fence(line, fence_char, fence_length):
                fence_char = ""
                continue
            content = _strip_columns(line, fence_indent)
            rendered.append(
                (LineStyle.PRE, 0, [(content, _NO_STYLE, "")] if content else [])
            )
            continue
        fence = _opening_fence(line)
        if fence is not None:
            fence_char, fence_length, fence_indent = fence
            levels.clear()
            continue
        rendered.append(_render_line(line, levels))

    while rendered and _is_blank(rendered[-1][2]):
        rendered.pop()

    segments: list[MdSegment] = []
    last = len(rendered) - 1
    for number, (style, indent, parts) in enumerate(rendered):
        if number < last:
            if parts and not parts[-1][1]:
                parts[-1] = (parts[-1][0] + "\n", _NO_STYLE, "")
            else:
                parts.append(("\n", _NO_STYLE, ""))
        segments.extend(
            MdSegment(text=piece, inline=styles, line=style, indent=indent, url=url)
            for piece, styles, url in parts
        )
    return tuple(segments)
