"""Neutralise CSV cells against formula injection (pure)."""

from __future__ import annotations

# A spreadsheet treats a cell starting with one of these as a formula, also
# after leading whitespace, so the check runs on the left-stripped value.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


def neutralize_cell(value: str) -> str:
    """``value`` with a leading ``'`` when a spreadsheet would run it as a formula."""
    if value.startswith(_FORMULA_PREFIXES) or value.lstrip().startswith(
        _FORMULA_PREFIXES
    ):
        return "'" + value
    return value
