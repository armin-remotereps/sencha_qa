"""Read-side DTOs of the case analysis, shared by services, views and templates."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CategoryCount:
    category: str
    label: str
    count: int
    critical: int
    untested: int


@dataclass(frozen=True)
class TriageBadge:
    category: str
    category_label: str
    severity: str
    severity_label: str
    # The case changed since it was classified.
    stale: bool


@dataclass(frozen=True)
class TriageCard:
    """The run page's "Case analysis" card."""

    test_run_id: int
    analysable_count: int
    classified_count: int
    stale_count: int
    # Analysable cases with no row yet.
    unclassified_count: int
    categories: tuple[CategoryCount, ...]
    # The run's active analysis thread, if any.
    thread_id: int | None
    # That thread's turn is queued or running.
    turn_live: bool
    # Why Analyze would be refused now ("" = allowed).
    analyze_blocker: str


@dataclass(frozen=True)
class ReportCase:
    pivot_id: int
    # "T4613470" or "R123": the reference the chat uses.
    ref: str
    # TestRail test URL, else the run case page path.
    link_url: str
    # "T4613470", else the case title.
    link_label: str
    title: str
    status_label: str
    severity: str
    severity_label: str
    finding: str
    untested: bool
    stale: bool
    # "" unless a person corrected the row in the chat.
    corrected_by_label: str


@dataclass(frozen=True)
class ReportGroup:
    title: str
    source: str
    cases: tuple[ReportCase, ...]
    # One level only: sub-groups have no sub-groups.
    sub_groups: tuple[ReportGroup, ...]

    @property
    def case_count(self) -> int:
        return len(self.cases) + sum(len(sub.cases) for sub in self.sub_groups)


@dataclass(frozen=True)
class ReportCategory:
    # 1-based position among the categories shown.
    number: int
    category: str
    label: str
    count: int
    critical: int
    untested: int
    note: str
    groups: tuple[ReportGroup, ...]
    # Classified but not (yet) in a group.
    ungrouped: tuple[ReportCase, ...]


@dataclass(frozen=True)
class TriageActions:
    """What the report page offers: the thread and whether Analyze is allowed."""

    thread_id: int | None
    turn_live: bool
    # Why Analyze would be refused now ("" = allowed).
    analyze_blocker: str


@dataclass(frozen=True)
class TriageReport:
    test_run_id: int
    run_name: str
    passed_count: int
    analysable_count: int
    not_finished_count: int
    classified_count: int
    stale_count: int
    unclassified_count: int
    turn_live: bool
    # Only categories with at least one case, in taxonomy order.
    categories: tuple[ReportCategory, ...]
    # Analysable cases with no row yet.
    unclassified: tuple[ReportCase, ...]
    actions: TriageActions
