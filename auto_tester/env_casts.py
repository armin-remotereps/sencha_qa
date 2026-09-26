from __future__ import annotations


def optional_float(value: str) -> float | None:
    """Cast an env value to float, treating an empty value as "not set" (None)."""
    stripped = value.strip()
    if not stripped:
        return None
    return float(stripped)
