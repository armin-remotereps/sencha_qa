"""Record stage-0 requirement statuses and a person's requirement edits.

Plan: docs/plan/feat-machine-setup-intake.md › D5, Services ›
requirement_status.py. A status is trusted until changed: it stays until a
check, the agent or a person records otherwise, stamped with the runner's
hostname (a different connected host reads it as unknown).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from django.utils import timezone

from accounts.models import CustomUser
from machine_readiness.exceptions import (
    MachineReadinessError,
    RequirementNotFoundError,
)
from machine_readiness.keys import normalise_key
from machine_readiness.models import (
    HOST_MAX_LENGTH,
    REQUIREMENT_STATUS_DETAIL_MAX_LENGTH,
    RequirementStatus,
    RequirementStatusSource,
    Stage0Requirement,
)
from machine_readiness.stage0_rules import requirement_errors
from machine_readiness.types import Stage0RequirementData

_STATUS_FIELDS = (
    "status",
    "status_detail",
    "status_source",
    "checked_at",
    "checked_host",
    "checked_by",
    "updated_at",
)
_NOT_FOUND = "That requirement is not in this project."


@dataclass(frozen=True)
class RequirementStatusUpdate:
    """One status to record for the requirement ``key``."""

    key: str
    # RequirementStatus value.
    status: str
    # Evidence, cut to REQUIREMENT_STATUS_DETAIL_MAX_LENGTH on one line.
    detail: str = ""


@dataclass(frozen=True)
class RecordStatusesOutcome:
    """``updated`` rows; keys not in the project and invalid statuses are
    reported, not fatal."""

    updated: int
    unknown_keys: tuple[str, ...] = ()
    invalid: tuple[str, ...] = ()


def record_statuses(
    *,
    project_id: int,
    items: Sequence[RequirementStatusUpdate],
    source: RequirementStatusSource,
    author: CustomUser | None,
    host: str,
) -> RecordStatusesOutcome:
    """Write the statuses of ``items`` (one query to load, one ``bulk_update``).

    A key given twice keeps its last status.
    """
    wanted: dict[str, RequirementStatusUpdate] = {}
    invalid: list[str] = []
    for item in items:
        key = normalise_key(item.key)
        if item.status not in RequirementStatus.values:
            invalid.append(
                f"«{key or item.key[:40]}»: unknown status «{item.status[:20]}»."
            )
            continue
        wanted[key] = item
    if not wanted:
        return RecordStatusesOutcome(updated=0, invalid=tuple(invalid))
    rows = list(
        Stage0Requirement.objects.filter(project_id=project_id, key__in=wanted).only(
            "id", "key", *_STATUS_FIELDS
        )
    )
    now = timezone.now()
    for row in rows:
        item = wanted[row.key]
        _apply_status(
            row,
            status=item.status,
            detail=item.detail,
            source=source,
            author=author,
            host=host,
            now=now,
        )
    if rows:
        Stage0Requirement.objects.bulk_update(rows, list(_STATUS_FIELDS))
    found = {row.key for row in rows}
    return RecordStatusesOutcome(
        updated=len(rows),
        unknown_keys=tuple(key for key in wanted if key not in found),
        invalid=tuple(invalid),
    )


def set_requirement_status(
    *,
    project_id: int,
    requirement_id: int,
    status: str,
    detail: str,
    author: CustomUser,
    host: str,
) -> None:
    """A person's status for one requirement (one query).

    Raises ``RequirementNotFoundError`` for another project's id and
    ``MachineReadinessError`` for an unknown status.
    """
    if status not in RequirementStatus.values:
        raise MachineReadinessError("Choose a valid status.")
    row = Stage0Requirement(id=requirement_id, project_id=project_id)
    _apply_status(
        row,
        status=status,
        detail=detail,
        source=RequirementStatusSource.PERSON,
        author=author,
        host=host,
        now=timezone.now(),
    )
    updated = Stage0Requirement.objects.filter(
        id=requirement_id, project_id=project_id
    ).update(**{name: getattr(row, name) for name in _STATUS_FIELDS})
    if not updated:
        raise RequirementNotFoundError(_NOT_FOUND)


def edit_requirement(
    *,
    project_id: int,
    requirement_id: int,
    title: str,
    kind: str,
    check_target: str,
    install_hint: str,
    ignored: bool,
) -> None:
    """A person's edit of one requirement's definition (key and conflicts kept).

    Two queries. Raises ``RequirementNotFoundError`` for another project's id
    and ``MachineReadinessError`` when the definition breaks a rule.
    """
    key = (
        Stage0Requirement.objects.filter(id=requirement_id, project_id=project_id)
        .values_list("key", flat=True)
        .first()
    )
    if key is None:
        raise RequirementNotFoundError(_NOT_FOUND)
    data = Stage0RequirementData(
        key=key,
        title=" ".join(title.split()),
        kind=kind,
        check_target=check_target.strip(),
        install_hint=install_hint.strip(),
    )
    errors = requirement_errors(data)
    if errors:
        raise MachineReadinessError(" ".join(errors))
    Stage0Requirement.objects.filter(id=requirement_id, project_id=project_id).update(
        title=data.title,
        kind=data.kind,
        check_target=data.check_target,
        install_hint=data.install_hint,
        ignored=ignored,
        updated_at=timezone.now(),
    )


def _apply_status(
    row: Stage0Requirement,
    *,
    status: str,
    detail: str,
    source: RequirementStatusSource,
    author: CustomUser | None,
    host: str,
    now: datetime,
) -> None:
    row.status = status
    row.status_detail = " ".join(detail.replace("\x00", "").split())[
        :REQUIREMENT_STATUS_DETAIL_MAX_LENGTH
    ]
    row.status_source = source
    row.checked_at = now
    row.checked_host = host.strip()[:HOST_MAX_LENGTH]
    row.checked_by = author
    row.updated_at = now
