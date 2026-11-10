"""Use case: one run's stage-0 checklist (the run page card, the agent tool).

Plan: docs/plan/feat-machine-setup-intake.md › Services › run_stage0.py.
Two queries (``run_stage0_state.load_run_stage0_rows``), no LLM. A status
recorded on another host than ``runner_host`` reads as unknown (D5); there
is no time-based expiry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from machine_readiness.models import RequirementKind, RequirementStatus
from machine_readiness.run_stage0_state import (
    Stage0Summary,
    host_mismatch,
    load_run_stage0_rows,
)

# Checklist order: what needs work first.
_STATUS_ORDER = {
    RequirementStatus.MISSING: 0,
    RequirementStatus.MISMATCH: 1,
    RequirementStatus.UNKNOWN: 2,
    RequirementStatus.PRESENT: 3,
}


@dataclass(frozen=True)
class RunStage0Item:
    """One requirement of the run, with its effective status."""

    key: str
    title: str
    # RequirementKind value and label.
    kind: str
    kind_label: str
    check_target: str
    install_hint: str
    # RequirementStatus value and label, after the host rule.
    status: str
    status_label: str
    # As stored; ``to_payload`` words a host mismatch instead.
    status_detail: str
    checked_at: datetime | None
    checked_host: str
    # The stored status was recorded on another machine.
    host_mismatch: bool
    case_count: int

    def _detail_text(self, show_host: bool) -> str:
        """The shown detail: the stored one, or, when the status was recorded
        on another machine, why it reads unknown (naming that machine only
        with ``show_host``)."""
        if not self.host_mismatch:
            return self.status_detail
        if show_host:
            return f"Last checked on {self.checked_host}, not on this machine."
        return "Last checked on another machine, not on this one."

    def to_payload(self, *, show_host: bool) -> dict[str, object]:
        """``show_host`` False blanks ``checked_host`` (read-only viewers,
        socket pushes); ``host_mismatch`` keeps its value."""
        return {
            "key": self.key,
            "title": self.title,
            "kind": self.kind,
            "kind_label": self.kind_label,
            "check_target": self.check_target,
            "install_hint": self.install_hint,
            "status": self.status,
            "status_label": self.status_label,
            "status_detail": self._detail_text(show_host),
            "checked_at": _iso(self.checked_at),
            "checked_host": self.checked_host if show_host else "",
            "host_mismatch": self.host_mismatch,
            "case_count": self.case_count,
        }


@dataclass(frozen=True)
class RunStage0Checklist:
    """What one run's cases need on the test machine."""

    run_id: int
    case_count: int
    unanalysed_case_count: int
    none_needed_case_count: int
    # Missing, mismatch, unknown, then present; by title within a status.
    items: tuple[RunStage0Item, ...]
    # The newest status of the items, and the host it was recorded on.
    last_checked_at: datetime | None
    last_checked_host: str
    # The connected runner's hostname; None when no runner is connected.
    runner_host: str | None

    def count(self, status: str) -> int:
        return sum(1 for item in self.items if item.status == status)

    @property
    def present_count(self) -> int:
        return self.count(RequirementStatus.PRESENT)

    @property
    def missing_count(self) -> int:
        return self.count(RequirementStatus.MISSING)

    @property
    def mismatch_count(self) -> int:
        return self.count(RequirementStatus.MISMATCH)

    @property
    def unknown_count(self) -> int:
        return self.count(RequirementStatus.UNKNOWN)

    def summary(self) -> Stage0Summary:
        """The "Machine prepared" counts, without querying again."""
        return Stage0Summary(
            case_count=self.case_count,
            unanalysed_case_count=self.unanalysed_case_count,
            requirement_count=len(self.items),
            present=self.present_count,
            missing=self.missing_count,
            mismatch=self.mismatch_count,
            unknown=self.unknown_count,
        )

    def to_payload(self, *, show_hosts: bool) -> dict[str, object]:
        """JSON for the run page (``json_script``) and the ``stage0`` event.

        ``show_hosts``: include machine hostnames (``runner_host``,
        ``last_checked_host``, each item's ``checked_host``): the page, for a
        viewer who can write. The ``stage0`` event goes to every viewer of
        the run, read-only members included, so it never carries them. The
        mismatch flags were computed with the hosts either way.
        """
        return {
            "run_id": self.run_id,
            "case_count": self.case_count,
            "unanalysed_case_count": self.unanalysed_case_count,
            "none_needed_case_count": self.none_needed_case_count,
            "present_count": self.present_count,
            "missing_count": self.missing_count,
            "mismatch_count": self.mismatch_count,
            "unknown_count": self.unknown_count,
            "last_checked_at": _iso(self.last_checked_at),
            "last_checked_host": self.last_checked_host if show_hosts else "",
            "runner_host": (self.runner_host or "") if show_hosts else "",
            "items": [item.to_payload(show_host=show_hosts) for item in self.items],
        }


def get_run_stage0(
    *, project_id: int, run_id: int, runner_host: str | None
) -> RunStage0Checklist:
    """The run's checklist (2 queries); ``runner_host`` from the connected runner."""
    rows = load_run_stage0_rows(project_id=project_id, run_id=run_id)
    items: list[RunStage0Item] = []
    for row in rows.requirements:
        mismatch = host_mismatch(row.checked_host, runner_host)
        status = (
            RequirementStatus.UNKNOWN
            if mismatch or row.stored_status not in RequirementStatus.values
            else RequirementStatus(row.stored_status)
        )
        items.append(
            RunStage0Item(
                key=row.key,
                title=row.title,
                kind=row.kind,
                kind_label=_kind_label(row.kind),
                check_target=row.check_target,
                install_hint=row.install_hint,
                status=status.value,
                status_label=status.label,
                status_detail=row.status_detail,
                checked_at=row.checked_at,
                checked_host=row.checked_host,
                host_mismatch=mismatch,
                case_count=row.case_count,
            )
        )
    items.sort(key=lambda item: _STATUS_ORDER.get(RequirementStatus(item.status), 9))
    newest = max(
        (row for row in rows.requirements if row.checked_at is not None),
        key=lambda row: row.checked_at or datetime.min,
        default=None,
    )
    return RunStage0Checklist(
        run_id=run_id,
        case_count=rows.case_count,
        unanalysed_case_count=rows.unanalysed_case_count,
        none_needed_case_count=rows.none_needed_case_count,
        items=tuple(items),
        last_checked_at=newest.checked_at if newest is not None else None,
        last_checked_host=newest.checked_host if newest is not None else "",
        runner_host=runner_host,
    )


def prepare_machine_text(*, run_name: str, run_id: int) -> str:
    """The server-built chat message of the run card's Prepare machine button."""
    name = " ".join(run_name.split())[:200]
    return f"Prepare the machine for run «{name}» (#{run_id})"


def _kind_label(kind: str) -> str:
    if kind in RequirementKind.values:
        return str(RequirementKind(kind).label)
    return kind or "Other"


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""
