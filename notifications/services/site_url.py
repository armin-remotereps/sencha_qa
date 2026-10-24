"""Absolute URLs for links that leave a request (notification clicks)."""

from __future__ import annotations

from django.conf import settings


def absolute_url(path: str) -> str | None:
    """``SITE_URL`` plus a same-origin ``path``, or ``None`` when unavailable.

    ``None`` when ``SITE_URL`` is empty or ``path`` is not a plain absolute
    path (``//host`` or a full URL would leave the site).
    """
    site_url = str(settings.SITE_URL).strip().rstrip("/")
    if not site_url:
        return None
    if not path.startswith("/") or path.startswith("//"):
        return None
    return site_url + path
