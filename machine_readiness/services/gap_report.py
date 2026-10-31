"""What is missing on the test machine, from the inventory and S0 statuses.

Plan: docs/plan/feat-machine-setup-intake.md › Services › gap_report.py.
No LLM and no runner call: the profile's items are matched against the stored
inventory of the runner's OS, and the stage-0 requirements are read with their
recorded statuses (a status checked on another host reads as unknown, D5).
With a ``run_id`` only the requirements that run's cases need are reported.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from django.db.models import Exists, OuterRef, QuerySet

from machine_readiness.models import (
    ArtifactInventory,
    ArtifactKind,
    ArtifactOs,
    InventorySource,
    PreconditionAnalysis,
    RequirementKind,
    RequirementStatus,
    Stage0Requirement,
)
from machine_readiness.profile_rules import artifact_os_for
from machine_readiness.profile_text import load_profile_snapshot
from machine_readiness.services.scan_artifacts import (
    InventorySummary,
    MatchRow,
    build_match_rows,
    data_line,
    fence_lines,
    match_profile,
    runner_host,
)
from machine_readiness.types import (
    InventoryEntry,
    MatchStatus,
    ProfileData,
    ProfileItemData,
)
from projects.models import Project, TestCase, TestRunTestCase

# get_gap_report's default text budget (plan: ≤ 12k characters).
GAP_REPORT_MAX_CHARS = 12_000


@dataclass(frozen=True)
class ArtifactGap:
    """A profile item that is missing, mismatched or not decidable."""

    key: str
    name: str
    kind_label: str
    path_pattern: str
    expected_version: str
    # Versions seen in the matching names ("" when none).
    candidates: str
    detail: str
    install_hint: str
    source: str


@dataclass(frozen=True)
class RequirementGap:
    """A stage-0 requirement that is not known to be in place."""

    id: int
    key: str
    title: str
    kind: str
    kind_label: str
    check_target: str
    install_hint: str
    # The effective status (unknown when checked on another host).
    status: str
    status_label: str
    status_detail: str
    checked_at: datetime | None
    checked_host: str
    # Why it reads as unknown, or how to check it (unknowns only).
    how_to_check: str


@dataclass(frozen=True)
class RequirementConflict:
    """Two requirements in scope that cannot be in place together."""

    first_key: str
    first_title: str
    second_key: str
    second_title: str


@dataclass(frozen=True)
class GapReport:
    """Gaps of the whole project, or of one run (``run_id``)."""

    run_id: int | None
    runner_os: ArtifactOs | None
    runner_connected: bool
    runner_host: str
    # The profile's base directory for ``runner_os``; "" when not set.
    base_dir: str
    profile_exists: bool
    inventory: InventorySummary | None
    match_rows: tuple[MatchRow, ...]
    missing_artifacts: tuple[ArtifactGap, ...]
    # Missing requirements that are not applications (paths, configs, ...).
    missing_requirements: tuple[RequirementGap, ...]
    apps_to_install: tuple[RequirementGap, ...]
    artifact_mismatches: tuple[ArtifactGap, ...]
    requirement_mismatches: tuple[RequirementGap, ...]
    conflicts: tuple[RequirementConflict, ...]
    unknown_artifacts: tuple[ArtifactGap, ...]
    unknown_requirements: tuple[RequirementGap, ...]
    # Requirements in scope (not ignored), and how many are present.
    requirement_count: int
    present_count: int
    # Cases (of the run, or of the project) whose preconditions have no
    # analysis yet.
    unanalysed_cases: int

    @property
    def runner_os_label(self) -> str:
        return str(self.runner_os.label) if self.runner_os is not None else ""

    @property
    def gap_count(self) -> int:
        return (
            len(self.missing_artifacts)
            + len(self.missing_requirements)
            + len(self.apps_to_install)
            + len(self.artifact_mismatches)
            + len(self.requirement_mismatches)
            + len(self.conflicts)
        )

    @property
    def unknown_count(self) -> int:
        return len(self.unknown_artifacts) + len(self.unknown_requirements)


def build_gap_report(*, project_id: int, run_id: int | None) -> GapReport:
    """The gap report; six queries at most (project, profile, items,
    inventory, requirements, unanalysed count). A ``run_id`` of another
    project reports nothing for the run's cases."""
    connected, system_info = (
        Project.objects.filter(pk=project_id)
        .values_list("agent_connected", "agent_system_info")
        .get()
    )
    os_raw = system_info.get("os") if isinstance(system_info, dict) else None
    runner_os = artifact_os_for(os_raw) if isinstance(os_raw, str) else None
    host = runner_host(system_info)
    snapshot = load_profile_snapshot(project_id)
    data = snapshot.data
    base_dir = data.base_dir_for(runner_os).strip() if runner_os is not None else ""

    inventory, entries = (
        _load_inventory(project_id, runner_os, with_entries=_needs_entries(data))
        if runner_os is not None
        else (None, ())
    )
    artifacts = _artifact_sections(data, runner_os, inventory, entries, base_dir)
    requirements = _requirement_rows(project_id, run_id)
    in_scope = {row.key: row for row in requirements}
    gaps = [_requirement_gap(row, host) for row in requirements]
    missing = [gap for gap in gaps if gap.status == RequirementStatus.MISSING]
    return GapReport(
        run_id=run_id,
        runner_os=runner_os,
        runner_connected=bool(connected),
        runner_host=host,
        base_dir=base_dir,
        profile_exists=snapshot.exists,
        inventory=inventory,
        match_rows=artifacts.match_rows,
        missing_artifacts=artifacts.missing,
        missing_requirements=tuple(
            gap for gap in missing if gap.kind != RequirementKind.APPLICATION
        ),
        apps_to_install=tuple(
            gap for gap in missing if gap.kind == RequirementKind.APPLICATION
        ),
        artifact_mismatches=artifacts.mismatched,
        requirement_mismatches=tuple(
            gap for gap in gaps if gap.status == RequirementStatus.MISMATCH
        ),
        conflicts=_conflicts(requirements, in_scope),
        unknown_artifacts=artifacts.unknown,
        unknown_requirements=tuple(
            gap for gap in gaps if gap.status == RequirementStatus.UNKNOWN
        ),
        requirement_count=len(gaps),
        present_count=sum(gap.status == RequirementStatus.PRESENT for gap in gaps),
        unanalysed_cases=_unanalysed_cases(project_id, run_id),
    )


def render_gap_report_text(report: GapReport, max_chars: int) -> str:
    """The gap report for an agent (pure); values inside a DATA fence."""
    scope = f"run #{report.run_id}" if report.run_id is not None else "the project"
    head = [f"Gap report for {scope}."]
    if report.runner_os is None:
        head.append("Test Runner OS: unknown (it has not reported one).")
    else:
        head.append(
            f"Test Runner OS: {report.runner_os_label}"
            + ("" if report.runner_connected else " (not connected now)")
            + "."
        )
    if not report.profile_exists:
        head.append("Intake is pending: there is no artifacts profile yet.")
    if report.inventory is None:
        head.append("Inventory: none for this OS yet; call scan_artifacts.")
    else:
        inv = report.inventory
        head.append(
            f"Inventory: {inv.entry_count} entries, scanned "
            f"{inv.scanned_at:%Y-%m-%d %H:%M} UTC"
            + (" (cut short)" if inv.truncated else "")
            + (" (failed, see the error below)" if inv.error else "")
            + "."
        )
    head.append(
        f"Requirements: {report.present_count} of {report.requirement_count} "
        f"present; cases not analysed yet: {report.unanalysed_cases}."
    )

    data: list[str] = []
    if report.base_dir:
        data.append(f"Base directory: {data_line(report.base_dir)}")
    if report.runner_host:
        data.append(f"Runner host: {data_line(report.runner_host)}")
    if report.inventory is not None and report.inventory.error:
        data.append(f"Scan error: {data_line(report.inventory.error)}")
    _section(data, "Missing artifacts", report.missing_artifacts, _artifact_line)
    _section(data, "Apps to install", report.apps_to_install, _requirement_line)
    _section(
        data,
        "Other missing requirements",
        report.missing_requirements,
        _requirement_line,
    )
    _section(
        data,
        "Version mismatches (artifacts)",
        report.artifact_mismatches,
        _artifact_line,
    )
    _section(
        data,
        "Version mismatches (requirements)",
        report.requirement_mismatches,
        _requirement_line,
    )
    _section(data, "Conflicting requirements", report.conflicts, _conflict_line)
    _section(data, "Unknown artifacts", report.unknown_artifacts, _artifact_line)
    _section(
        data,
        "Unknown requirements (how to check)",
        report.unknown_requirements,
        _unknown_line,
    )
    if not data:
        data.append("No gaps recorded.")
    tail = (
        f"{report.unanalysed_cases} cases are not analysed yet; call "
        "analyse_preconditions to include their requirements."
        if report.unanalysed_cases
        else ""
    )
    return fence_lines("\n".join(head), data, tail, max_chars)


@dataclass(frozen=True)
class _ArtifactSections:
    match_rows: tuple[MatchRow, ...] = ()
    missing: tuple[ArtifactGap, ...] = ()
    mismatched: tuple[ArtifactGap, ...] = ()
    unknown: tuple[ArtifactGap, ...] = ()


def _needs_entries(data: ProfileData) -> bool:
    return any(item.path_pattern.strip() for item in data.items)


def _load_inventory(
    project_id: int, os_name: ArtifactOs, *, with_entries: bool
) -> tuple[InventorySummary | None, tuple[InventoryEntry, ...]]:
    """The OS's inventory; its entries only when ``with_entries`` (one query)."""
    rows = ArtifactInventory.objects.filter(
        project_id=project_id, os_name=os_name
    ).select_related("scanned_by")
    if not with_entries:
        rows = rows.defer("entries")
    row = rows.first()
    if row is None:
        return None, ()
    entries: tuple[InventoryEntry, ...] = ()
    if with_entries and isinstance(row.entries, list):
        entries = tuple(
            entry
            for entry in (InventoryEntry.from_json(raw) for raw in row.entries)
            if entry is not None
        )
    user = row.scanned_by
    summary = InventorySummary(
        os_name=os_name,
        base_dir=row.base_dir,
        host=row.host,
        scanned_at=row.scanned_at,
        source_label=(
            str(InventorySource(row.source).label)
            if row.source in InventorySource.values
            else ""
        ),
        scanned_by_name=(
            (user.get_full_name().strip() or user.email) if user is not None else ""
        ),
        entry_count=row.entry_count,
        truncated=row.truncated,
        permission_errors=row.permission_errors,
        error=row.error,
    )
    return summary, entries


def _artifact_sections(
    data: ProfileData,
    runner_os: ArtifactOs | None,
    inventory: InventorySummary | None,
    entries: Sequence[InventoryEntry],
    base_dir: str,
) -> _ArtifactSections:
    if not data.items:
        return _ArtifactSections()
    if runner_os is None or inventory is None or inventory.error:
        why = (
            "The Test Runner has not reported its OS."
            if runner_os is None
            else (
                "The last scan failed."
                if inventory is not None
                else "The base directory has not been scanned."
            )
        )
        return _ArtifactSections(
            unknown=tuple(_artifact_gap(item, "", why) for item in data.items)
        )
    matches = match_profile(
        data,
        runner_os,
        entries,
        base_dir=inventory.base_dir or base_dir,
        truncated=inventory.truncated,
    )
    rows = build_match_rows(data, matches)
    items = {item.key: item for item in data.items}
    missing: list[ArtifactGap] = []
    mismatched: list[ArtifactGap] = []
    unknown: list[ArtifactGap] = []
    for row in rows:
        item = items[row.key]
        if row.status == MatchStatus.MISSING:
            missing.append(_artifact_gap(item, row.candidates, row.detail))
        elif row.status == MatchStatus.VERSION_MISMATCH:
            mismatched.append(_artifact_gap(item, row.candidates, row.detail))
        elif row.status in (MatchStatus.AMBIGUOUS, MatchStatus.UNCHECKED):
            unknown.append(_artifact_gap(item, row.candidates, row.detail))
    return _ArtifactSections(
        match_rows=rows,
        missing=tuple(missing),
        mismatched=tuple(mismatched),
        unknown=tuple(unknown),
    )


def _artifact_gap(item: ProfileItemData, candidates: str, detail: str) -> ArtifactGap:
    return ArtifactGap(
        key=item.key,
        name=item.name,
        kind_label=(
            str(ArtifactKind(item.kind).label)
            if item.kind in ArtifactKind.values
            else item.kind
        ),
        path_pattern=item.path_pattern,
        expected_version=item.expected_version,
        candidates=candidates,
        detail=detail,
        install_hint=item.install_hint,
        source=item.source,
    )


def _requirement_rows(project_id: int, run_id: int | None) -> list[Stage0Requirement]:
    """Requirements in scope, not ignored, by key (one query)."""
    rows: QuerySet[Stage0Requirement] = Stage0Requirement.objects.filter(
        project_id=project_id, ignored=False
    )
    if run_id is not None:
        md5s = (
            TestCase.objects.filter(project_id=project_id)
            .filter(_in_run(project_id, run_id))
            .values("preconditions_md5")
        )
        rows = rows.filter(
            analyses__project_id=project_id, analyses__preconditions_md5__in=md5s
        ).distinct()
    return list(
        rows.only(
            "id",
            "key",
            "title",
            "kind",
            "check_target",
            "install_hint",
            "conflicts_with",
            "status",
            "status_detail",
            "checked_at",
            "checked_host",
        ).order_by("key")
    )


def _in_run(project_id: int, run_id: int) -> Exists:
    """A test case is in the run; never true for another project's run."""
    return Exists(
        TestRunTestCase.objects.filter(
            test_case_id=OuterRef("pk"),
            test_run_id=run_id,
            test_run__project_id=project_id,
        )
    )


def _unanalysed_cases(project_id: int, run_id: int | None) -> int:
    analysed = PreconditionAnalysis.objects.filter(project_id=project_id).values(
        "preconditions_md5"
    )
    cases = TestCase.objects.filter(project_id=project_id).exclude(
        preconditions_md5__in=analysed
    )
    if run_id is not None:
        cases = cases.filter(_in_run(project_id, run_id))
    return cases.count()


def _requirement_gap(row: Stage0Requirement, runner_host_name: str) -> RequirementGap:
    status = row.status if row.status in RequirementStatus.values else ""
    other_host = bool(
        runner_host_name
        and row.checked_host
        and row.checked_host.lower() != runner_host_name.lower()
    )
    if other_host or not status:
        status = RequirementStatus.UNKNOWN
    how = ""
    if status == RequirementStatus.UNKNOWN:
        how = (
            f"Recorded on another machine ({row.checked_host}); check it again here."
            if other_host
            else _how_to_check(row)
        )
    return RequirementGap(
        id=row.pk,
        key=row.key,
        title=row.title,
        kind=row.kind,
        kind_label=(
            str(RequirementKind(row.kind).label)
            if row.kind in RequirementKind.values
            else row.kind
        ),
        check_target=row.check_target,
        install_hint=row.install_hint,
        status=status,
        status_label=str(RequirementStatus(status).label),
        status_detail="" if other_host else row.status_detail,
        checked_at=row.checked_at,
        checked_host=row.checked_host,
        how_to_check=how,
    )


def _how_to_check(row: Stage0Requirement) -> str:
    target = row.check_target.strip()
    if row.kind == RequirementKind.APPLICATION:
        return (
            f"Check machine on a run checks whether «{target}» is installed; "
            "a read-only helper can also report its version."
            if target
            else "Ask a read-only helper whether the application is installed."
        )
    if row.kind == RequirementKind.ARTIFACT:
        return (
            f"Scan the base directory and look for the artifact «{target}»."
            if target
            else "Scan the base directory."
        )
    if row.kind == RequirementKind.PATH:
        return (
            f"Check machine on a run checks whether «{target}» exists."
            if target
            else "Ask a read-only helper to look for the path."
        )
    return "Ask a read-only helper to check it on the test machine."


def _conflicts(
    rows: Sequence[Stage0Requirement], in_scope: dict[str, Stage0Requirement]
) -> tuple[RequirementConflict, ...]:
    """Pairs in scope where one lists the other in ``conflicts_with``."""
    pairs: dict[tuple[str, str], RequirementConflict] = {}
    for row in rows:
        others = row.conflicts_with if isinstance(row.conflicts_with, list) else []
        for other_key in others:
            other = in_scope.get(other_key) if isinstance(other_key, str) else None
            if other is None or other.key == row.key:
                continue
            first, second = sorted((row, other), key=lambda item: item.key)
            pairs.setdefault(
                (first.key, second.key),
                RequirementConflict(
                    first_key=first.key,
                    first_title=first.title,
                    second_key=second.key,
                    second_title=second.title,
                ),
            )
    return tuple(pairs[key] for key in sorted(pairs))


def _section[T](
    data: list[str], title: str, rows: Sequence[T], line: Callable[[T], str]
) -> None:
    if not rows:
        return
    if data:
        data.append("")
    data.append(f"{title} ({len(rows)}):")
    data.extend(line(row) for row in rows)


def _artifact_line(gap: ArtifactGap) -> str:
    parts = [f"- {data_line(gap.name)} [{gap.key}] ({gap.kind_label})"]
    if gap.path_pattern:
        parts.append(f"path {data_line(gap.path_pattern)}")
    if gap.expected_version:
        parts.append(f"expected {data_line(gap.expected_version)}")
    if gap.candidates:
        parts.append(f"versions seen {gap.candidates}")
    if gap.detail:
        parts.append(data_line(gap.detail))
    if gap.source:
        parts.append(f"source {data_line(gap.source)}")
    if gap.install_hint:
        parts.append(f"install hint {data_line(gap.install_hint)}")
    return "; ".join(parts)


def _requirement_line(gap: RequirementGap) -> str:
    parts = [f"- {data_line(gap.title)} [{gap.key}] ({gap.kind_label})"]
    if gap.check_target:
        parts.append(f"target {data_line(gap.check_target)}")
    if gap.status_detail:
        parts.append(f"evidence {data_line(gap.status_detail)}")
    if gap.install_hint:
        parts.append(f"install hint {data_line(gap.install_hint)}")
    return "; ".join(parts)


def _unknown_line(gap: RequirementGap) -> str:
    return f"{_requirement_line(gap)}; how to check: {data_line(gap.how_to_check)}"


def _conflict_line(conflict: RequirementConflict) -> str:
    return (
        f"- {data_line(conflict.first_title)} [{conflict.first_key}] conflicts "
        f"with {data_line(conflict.second_title)} [{conflict.second_key}]"
    )
