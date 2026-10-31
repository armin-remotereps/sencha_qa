"""HTML to text with the stdlib parser; ``script`` and ``style`` are dropped.

Headings ``h1``..``h3`` start sections ("section 'Install the agent'").
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from knowledge_base.chunking import SectionCollector, quoted

_DROPPED = frozenset({"script", "style", "noscript", "template", "svg"})
_SECTION_HEADINGS = frozenset({"h1", "h2", "h3"})
_BLOCKS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tr",
        "ul",
    }
)
_CELLS = frozenset({"td", "th"})
_SPACES = re.compile(r"[^\S\t\n]+")


class _Stripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        # (heading, text) per section, the first one without a heading.
        self.sections: list[tuple[str, list[str]]] = [("", [])]
        self.title: list[str] = []
        self._dropped_depth = 0
        self._in_title = False
        self._heading: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROPPED:
            self._dropped_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if self._dropped_depth:
            return
        if tag in _SECTION_HEADINGS:
            self._heading = []
            self.sections.append(("", []))
        if tag in _BLOCKS:
            self._text("\n")
        elif tag in _CELLS:
            self._text("\t")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROPPED:
            self._dropped_depth = max(self._dropped_depth - 1, 0)
            return
        if tag == "title":
            self._in_title = False
        if self._dropped_depth:
            return
        if tag in _SECTION_HEADINGS and self._heading is not None:
            heading = " ".join("".join(self._heading).split())
            self.sections[-1] = (heading, self.sections[-1][1])
            self._heading = None
        if tag in _BLOCKS:
            self._text("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)
        if self._dropped_depth:
            return
        if self._heading is not None:
            self._heading.append(data)
        self._text(data)

    def _text(self, text: str) -> None:
        self.sections[-1][1].append(text)


def extract_html(text: str, collector: SectionCollector) -> None:
    stripper = _Stripper()
    stripper.feed(text)
    stripper.close()
    title = " ".join("".join(stripper.title).split())
    for index, (heading, parts) in enumerate(stripper.sections):
        body = _tidy("".join(parts))
        if not body:
            continue
        if heading:
            location = f"section {quoted(heading)}"
        elif index == 0:
            location = "start of page"
        else:
            location = f"part {index + 1}"
        if not collector.add(location, heading or title, body):
            return


def _tidy(text: str) -> str:
    # Collapse spaces but keep the tabs between table cells.
    lines = (_SPACES.sub(" ", line).strip() for line in text.split("\n"))
    tidy: list[str] = []
    for line in lines:
        if line or (tidy and tidy[-1]):
            tidy.append(line)
    return "\n".join(tidy).strip()
