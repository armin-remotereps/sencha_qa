"""The fixed taxonomy as the AI calls receive it, plus label lookups (pure)."""

from __future__ import annotations

from agents.triage_types import TriageTaxonomyText
from run_triage.taxonomy import (
    CATEGORY_MEANINGS,
    SEVERITY_MEANINGS,
    TriageCategory,
    TriageSeverity,
)

CATEGORY_ORDER: tuple[str, ...] = tuple(str(value) for value in TriageCategory.values)
SEVERITY_ORDER: tuple[str, ...] = tuple(str(value) for value in TriageSeverity.values)


def category_entry(category: str) -> tuple[str, str, str]:
    """(value, label, meaning) of a category."""
    return (category, category_label(category), CATEGORY_MEANINGS[category])


def triage_taxonomy() -> TriageTaxonomyText:
    return TriageTaxonomyText(
        categories=tuple(category_entry(value) for value in CATEGORY_ORDER),
        severities=tuple(
            (value, severity_label(value), SEVERITY_MEANINGS[value])
            for value in SEVERITY_ORDER
        ),
    )


def category_label(category: str) -> str:
    if category in CATEGORY_ORDER:
        return str(TriageCategory(category).label)
    return category


def severity_label(severity: str) -> str:
    if severity in SEVERITY_ORDER:
        return str(TriageSeverity(severity).label)
    return severity


def severity_rank(severity: str) -> int:
    """0 for critical, most severe first; unknown values last."""
    return (
        SEVERITY_ORDER.index(severity)
        if severity in SEVERITY_ORDER
        else len(SEVERITY_ORDER)
    )
