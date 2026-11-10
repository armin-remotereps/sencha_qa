"""Shared value types of the superuser "All projects" page (service, form, view)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# Longest search text the page accepts; longer input is cut, not refused.
ALL_PROJECTS_QUERY_MAX_LENGTH = 100


class ProjectStatusFilter(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    ALL = "all"

    @property
    def label(self) -> str:
        return _STATUS_LABELS[self]


class RunnerFilter(StrEnum):
    ANY = "any"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"

    @property
    def label(self) -> str:
        return _RUNNER_LABELS[self]


class RunActivityFilter(StrEnum):
    ANY = "any"
    ACTIVE = "active"
    IDLE = "idle"

    @property
    def label(self) -> str:
        return _RUN_LABELS[self]


class MembershipFilter(StrEnum):
    ANY = "any"
    MINE = "mine"
    NOT_MINE = "not_mine"

    @property
    def label(self) -> str:
        return _MEMBERSHIP_LABELS[self]


class AllProjectsSort(StrEnum):
    NEWEST = "newest"
    NAME = "name"
    ACTIVITY = "activity"

    @property
    def label(self) -> str:
        return _SORT_LABELS[self]


_STATUS_LABELS: dict[ProjectStatusFilter, str] = {
    ProjectStatusFilter.ACTIVE: "Active",
    ProjectStatusFilter.ARCHIVED: "Archived",
    ProjectStatusFilter.ALL: "Active and archived",
}
_RUNNER_LABELS: dict[RunnerFilter, str] = {
    RunnerFilter.ANY: "Any",
    RunnerFilter.CONNECTED: "Active run on a connected runner",
    RunnerFilter.DISCONNECTED: "No active run on a connected runner",
}
_RUN_LABELS: dict[RunActivityFilter, str] = {
    RunActivityFilter.ANY: "Any",
    RunActivityFilter.ACTIVE: "Run in progress",
    RunActivityFilter.IDLE: "No run in progress",
}
_MEMBERSHIP_LABELS: dict[MembershipFilter, str] = {
    MembershipFilter.ANY: "Any",
    MembershipFilter.MINE: "I am a member",
    MembershipFilter.NOT_MINE: "I am not a member",
}
_SORT_LABELS: dict[AllProjectsSort, str] = {
    AllProjectsSort.NEWEST: "Newest first",
    AllProjectsSort.NAME: "Name",
    AllProjectsSort.ACTIVITY: "Latest run first",
}


@dataclass(frozen=True)
class AllProjectsFilters:
    """The cleaned filters; the defaults are the unfiltered page."""

    query: str = ""
    status: ProjectStatusFilter = ProjectStatusFilter.ACTIVE
    runner: RunnerFilter = RunnerFilter.ANY
    run: RunActivityFilter = RunActivityFilter.ANY
    membership: MembershipFilter = MembershipFilter.ANY
    sort: AllProjectsSort = AllProjectsSort.NEWEST

    @property
    def is_default(self) -> bool:
        return self == AllProjectsFilters()
