"""Data passed to and from the run case classifier and grouper (no ORM).

Taxonomy values travel as plain strings so ``agents`` never imports
``run_triage``; the caller passes the allowed values and their meanings.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TriageTaxonomyText:
    """Allowed categories and severities, each (value, label, meaning), in order."""

    categories: tuple[tuple[str, str, str], ...]
    severities: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class TriageShot:
    """One screenshot sent with a case (already downscaled)."""

    screenshot_id: int
    step_num: int | None
    base64_data: str
    media_type: str


@dataclass(frozen=True)
class TriageCaseInput:
    """One case as the classifier sees it; every text is data, never instructions."""

    # Opaque reference the answer must echo ("T4613470" or "R123").
    ref: str
    title: str
    # The run case status label ("Failed", "Blocked", "Needs re-test", ...).
    status_label: str
    # Case text (preconditions, steps, expected results), capped.
    case_text: str
    # The verdict text and step breakdown, capped.
    result_text: str
    # Last characters of the case log ("" when none).
    log_tail: str
    screenshots: tuple[TriageShot, ...]


@dataclass(frozen=True)
class CaseClassification:
    ref: str
    category: str
    severity: str
    finding: str


@dataclass(frozen=True)
class GroupItem:
    """One classified case as the grouper sees it."""

    ref: str
    severity: str
    finding: str
    title: str


@dataclass(frozen=True)
class SubGroupResult:
    title: str
    refs: tuple[str, ...]


@dataclass(frozen=True)
class GroupResult:
    """A group's finding with its direct cases and optional sub-groups."""

    title: str
    refs: tuple[str, ...]
    sub_groups: tuple[SubGroupResult, ...]


@dataclass(frozen=True)
class CategoryGrouping:
    category: str
    note: str
    groups: tuple[GroupResult, ...]
