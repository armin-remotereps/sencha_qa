from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from django import template

from auto_tester.durations import format_duration

register = template.Library()


@register.filter
def get_item(mapping: Mapping[Any, Any] | None, key: Any) -> Any:
    """Look up `key` in `mapping`, returning None when absent or `mapping` is None.

    Django has no built-in dict-lookup template filter, so templates use
    this to read a per-project value (e.g. a latest-run lookup keyed by
    project id) out of a dict passed in the view context. Typed with
    `Any` because template filters receive untyped values from the
    template context by nature.
    """
    if mapping is None:
        return None
    return mapping.get(key)


@register.filter
def duration(value: timedelta | None) -> str:
    """Render a duration with :func:`auto_tester.durations.format_duration`."""
    return format_duration(value)
