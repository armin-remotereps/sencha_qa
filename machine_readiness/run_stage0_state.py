"""A run's stage-0 requirements as stored, and their summary for readiness.

A leaf module (models plus pure rules), so ``projects.services`` can add the
"Machine prepared" warning to "Ready to run?" without importing a
``machine_readiness`` service package. Plan:
docs/plan/feat-machine-setup-intake.md › D5, Services › run_stage0_state.py.

Statuses are trusted until changed (D5): a status recorded on another host
than the connected runner's reads as unknown; there is no time-based expiry.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime

from django.db.models import Count

from machine_readiness.models import (
    HOST_MAX_LENGTH,
    PreconditionAnalysis,
    RequirementStatus,
)
from projects.models import Project, TestRunTestCase


@dataclass(frozen=True)
class RunRequirementRow:
    """One (not ignored) requirement cited by the run's analysed cases."""

    id: int
    key: str
    title: str
    # RequirementKind value.
    kind: str
    check_target: str
    install_hint: str
    # As stored, before the host rule.
    stored_status: str
    status_detail: str
    status_source: str
    checked_at: datetime | None
    checked_host: str
    # Cases of the run whose preconditions cite it.
    case_count: int


@dataclass(frozen=True)
class RunStage0Rows:
    """What the run's cases need, as stored (two queries)."""

    case_count: int
    unanalysed_case_count: int
    # Analysed cases whose preconditions need nothing on the machine.
    none_needed_case_count: int
    # Ordered by title.
    requirements: tuple[RunRequirementRow, ...]


@dataclass(frozen=True)
class Stage0Summary:
    """Counts for the "Machine prepared" readiness warning."""

    case_count: int
    unanalysed_case_count: int
    requirement_count: int
    present: int
    missing: int
    mismatch: int
    unknown: int

    @property
    def prepared(self) -> bool:
        """Every case analysed and every requirement present."""
        return self.unanalysed_case_count == 0 and self.present == (
            self.requirement_count
        )


def normalise_host(host: str) -> str:
    """A hostname as stored and compared: trimmed, at most HOST_MAX_LENGTH."""
    return host.strip()[:HOST_MAX_LENGTH]


def runner_host(system_info: object) -> str | None:
    """The connected runner's hostname from ``Project.agent_system_info``.

    None when no runner is connected (the field is cleared on disconnect) or
    it reported no hostname: statuses are then shown as stored. The one
    definition every reader (gap report, scan, quick check, views) uses.
    """
    if not isinstance(system_info, Mapping):
        return None
    host = system_info.get("hostname")
    if not isinstance(host, str):
        return None
    return normalise_host(host) or None


def host_mismatch(checked_host: str, current_host: str | None) -> bool:
    """True when the status was recorded on another machine (D5).

    Compared only when both are known (a status recorded without a host, by
    a person with no runner connected, is trusted as stored), normalised and
    case-insensitively: hostnames are not case-sensitive.
    """
    checked = normalise_host(checked_host).casefold()
    current = normalise_host(current_host or "").casefold()
    return bool(checked and current and checked != current)


def effective_status(row: RunRequirementRow, current_host: str | None) -> str:
    """The stored status, or unknown when it was recorded on another host."""
    if host_mismatch(row.checked_host, current_host):
        return RequirementStatus.UNKNOWN
    return row.stored_status


def load_run_stage0_rows(*, project_id: int, run_id: int) -> RunStage0Rows:
    """The run's distinct precondition texts and their analyses (2 queries).

    Query 1: the md5 of each of the run's cases' preconditions, with counts.
    Query 2: the analyses of those md5 values with their requirements' fields
    (one row per analysis and requirement; a left join, so analyses citing
    none still show).
    """
    md5_counts: dict[str, int] = {
        row["test_case__preconditions_md5"]: row["cases"]
        for row in TestRunTestCase.objects.filter(
            test_run_id=run_id, test_run__project_id=project_id
        )
        .values("test_case__preconditions_md5")
        .annotate(cases=Count("id"))
        .order_by()
    }
    case_count = sum(md5_counts.values())
    if not md5_counts:
        return RunStage0Rows(0, 0, 0, ())
    analysis_rows = (
        PreconditionAnalysis.objects.filter(
            project_id=project_id, preconditions_md5__in=list(md5_counts)
        )
        .values_list(
            "preconditions_md5",
            "requirements__id",
            "requirements__key",
            "requirements__title",
            "requirements__kind",
            "requirements__check_target",
            "requirements__install_hint",
            "requirements__ignored",
            "requirements__status",
            "requirements__status_detail",
            "requirements__status_source",
            "requirements__checked_at",
            "requirements__checked_host",
        )
        .order_by()
    )
    analysed: set[str] = set()
    # md5 -> ids of the requirements it cites (not ignored).
    cited: dict[str, set[int]] = {}
    found: dict[int, RunRequirementRow] = {}
    req_cases: dict[int, int] = {}
    for (
        md5,
        req_id,
        key,
        title,
        kind,
        check_target,
        install_hint,
        ignored,
        status,
        status_detail,
        status_source,
        checked_at,
        checked_host,
    ) in analysis_rows:
        analysed.add(md5)
        if req_id is None or ignored:
            continue
        cited.setdefault(md5, set()).add(req_id)
        req_cases[req_id] = req_cases.get(req_id, 0) + md5_counts.get(md5, 0)
        found[req_id] = RunRequirementRow(
            id=req_id,
            key=key,
            title=title,
            kind=kind,
            check_target=check_target,
            install_hint=install_hint,
            stored_status=status,
            status_detail=status_detail,
            status_source=status_source,
            checked_at=checked_at,
            checked_host=checked_host,
            case_count=0,
        )
    unanalysed = sum(n for md5, n in md5_counts.items() if md5 not in analysed)
    none_needed = sum(
        n for md5, n in md5_counts.items() if md5 in analysed and not cited.get(md5)
    )
    requirements = tuple(
        sorted(
            (
                replace(row, case_count=req_cases[req_id])
                for req_id, row in found.items()
            ),
            key=lambda item: (item.title.lower(), item.key),
        )
    )
    return RunStage0Rows(
        case_count=case_count,
        unanalysed_case_count=unanalysed,
        none_needed_case_count=none_needed,
        requirements=requirements,
    )


def run_stage0_summary(project: Project, run_id: int) -> Stage0Summary:
    """Counts of the run's requirements by effective status (2 queries).

    The runner host comes from ``project.agent_system_info`` (no query).
    """
    rows = load_run_stage0_rows(project_id=project.id, run_id=run_id)
    host = runner_host(project.agent_system_info)
    counts = {status: 0 for status in RequirementStatus.values}
    for row in rows.requirements:
        status = effective_status(row, host)
        counts[status if status in counts else RequirementStatus.UNKNOWN] += 1
    return Stage0Summary(
        case_count=rows.case_count,
        unanalysed_case_count=rows.unanalysed_case_count,
        requirement_count=len(rows.requirements),
        present=counts[RequirementStatus.PRESENT],
        missing=counts[RequirementStatus.MISSING],
        mismatch=counts[RequirementStatus.MISMATCH],
        unknown=counts[RequirementStatus.UNKNOWN],
    )
