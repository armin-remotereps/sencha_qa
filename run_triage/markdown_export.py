"""The case analysis as Markdown, in the shape people paste into reports:

    1. Product bug: functional (40, 15 critical)
    <note>
    * <finding>: [T4613470](url), [T4613471](url)
      * <sub-group>: [T4613472](url)

Pure: built from the ``TriageReport`` DTO, no queries.
"""

from __future__ import annotations

from collections.abc import Sequence

from django.conf import settings

from run_triage.tool_text import count_suffix, one_line
from run_triage.types import ReportCase, ReportCategory, ReportGroup, TriageReport


def triage_markdown(report: TriageReport) -> str:
    lines: list[str] = []
    for category in report.categories:
        if lines:
            lines.append("")
        lines.extend(_category_lines(category))
    if report.unclassified:
        if lines:
            lines.append("")
        lines.append(f"Not classified yet ({len(report.unclassified)})")
        lines.append(f"* {_links(report.unclassified)}")
    return "\n".join(lines) + ("\n" if lines else "")


def _category_lines(category: ReportCategory) -> list[str]:
    lines = [
        f"{category.number}. {category.label} "
        f"{count_suffix(category.count, category.critical, category.untested)}"
    ]
    note = category.note.strip()
    if note:
        lines.append(note)
    for group in category.groups:
        lines.extend(_group_lines(group))
    if category.ungrouped:
        lines.append(f"* Not grouped: {_links(category.ungrouped)}")
    return lines


def _group_lines(group: ReportGroup) -> list[str]:
    title = _escape_text(one_line(group.title))
    head = f"* {title}"
    if group.cases:
        head += f": {_links(group.cases)}"
    lines = [head]
    for sub in group.sub_groups:
        lines.append(f"  * {_escape_text(one_line(sub.title))}: {_links(sub.cases)}")
    return lines


def _links(cases: Sequence[ReportCase]) -> str:
    return ", ".join(_link(case) for case in cases)


def _link(case: ReportCase) -> str:
    label = _escape_label(one_line(case.link_label))
    if case.untested:
        label += " (Untested)"
    return f"[{label}]({_absolute(case.link_url)})"


def _absolute(url: str) -> str:
    """A run case page path with ``SITE_URL`` in front (when configured)."""
    if url.startswith("/") and settings.SITE_URL:
        url = settings.SITE_URL.rstrip("/") + url
    return url.replace(" ", "%20").replace(")", "%29").replace("(", "%28")


def _escape_label(text: str) -> str:
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _escape_text(text: str) -> str:
    """Text that cannot open a link or a list item of its own."""
    return _escape_label(text)
