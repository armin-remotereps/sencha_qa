"""Neutralise CSV cells against formula injection (pure)."""

from __future__ import annotations

# A spreadsheet treats a cell starting with one of these as a formula.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def neutralize_cell(value: str) -> str:
    """``value`` with a leading ``'`` when a spreadsheet would run it as a formula."""
    if value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value
