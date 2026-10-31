"""Template helpers of the Knowledge base tab."""

from __future__ import annotations

import re

from django import template
from django.utils.html import conditional_escape, format_html
from django.utils.safestring import SafeString

register = template.Library()

# A search snippet marks each match as «match»; everything is plain text.
_MATCH = re.compile(r"«(.*?)»", re.DOTALL)


@register.filter
def highlight_snippet(snippet: str) -> SafeString:
    """Escape a search snippet and wrap each «match» in ``<mark>``.

    Every piece of the snippet is escaped; the only markup is the ``<mark>``
    element built by ``format_html``. A stray, unpaired « or » stays as text.
    """
    parts: list[str] = []
    position = 0
    for match in _MATCH.finditer(snippet):
        parts.append(conditional_escape(snippet[position : match.start()]))
        parts.append(format_html("<mark>{}</mark>", match.group(1)))
        position = match.end()
    parts.append(conditional_escape(snippet[position:]))
    return format_html("{}" * len(parts), *parts)
