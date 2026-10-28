"""Request helpers for paginated lists: page and per-page parsing, page links.

Pure request utilities shared by list views; they never touch the ORM.
"""

from __future__ import annotations

from typing import Any

from django.core.paginator import Page
from django.http import HttpRequest

ALLOWED_PER_PAGE = [10, 20, 50, 100]
DEFAULT_PER_PAGE = 20


def parse_per_page(request: HttpRequest, default: int = DEFAULT_PER_PAGE) -> int:
    """The ``per_page`` query value when it is one of ``ALLOWED_PER_PAGE``."""
    try:
        value = int(request.GET.get("per_page", ""))
    except ValueError:
        return default
    return value if value in ALLOWED_PER_PAGE else default


def parse_page(request: HttpRequest) -> int:
    """The 1-based ``page`` query value; 1 when missing or invalid."""
    try:
        return max(1, int(request.GET.get("page", "1")))
    except ValueError:
        return 1


# Page[Any]: the helper only reads page numbers, whatever the page holds.
def elided_page_range(page_obj: Page[Any]) -> list[int | str]:
    """Page numbers to link, with "…" gaps, around the current page."""
    return [
        int(p) if isinstance(p, int) else str(p)
        for p in page_obj.paginator.get_elided_page_range(
            page_obj.number, on_each_side=1, on_ends=1
        )
    ]
