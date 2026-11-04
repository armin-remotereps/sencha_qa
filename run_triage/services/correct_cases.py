"""A person's corrections of the case analysis, made through the analysis chat.

The agent calls these only on the person's request in the current turn.
Corrections apply directly (the analysis is advisory: no verdict or TestRail
change), are marked with who asked and when, and a later classification keeps
them unless the case changed. Every write is gated on the turn still running.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from django.db import transaction
from django.utils import timezone

from agents.triage_types import GroupResult
from projects.models import TestRunTestCase
from run_triage.case_queries import analysable_q
from run_triage.case_refs import normalize_ref, run_ref_map
from run_triage.case_scope import is_untested
from run_triage.exceptions import RunTriageError
from run_triage.models import RunTriage, TriageCase, TriageGroup
from run_triage.services.agent_texts import triage_outline_text
from run_triage.services.group_writes import (
    PlannedGroup,
    PlannedSubGroup,
    create_groups,
    group_title,
    write_note,
)
from run_triage.services.taxonomy_text import (
    CATEGORY_ORDER,
    SEVERITY_ORDER,
    category_label,
    severity_label,
)
from run_triage.services.turn_gate import require_running_turn
from run_triage.taxonomy import (
    TRIAGE_FINDING_MAX_LENGTH,
    TRIAGE_NOTE_MAX_LENGTH,
    TriageSource,
)
from run_triage.tool_text import cut, one_line, plural

MAX_CHANGES_PER_CALL: Final = 200
MAX_GROUPS_PER_CATEGORY: Final = 200
_REASON_MAX: Final = 500
_LABEL_MAX: Final = 200
_ROW_FIELDS: Final = (
    "category",
    "severity",
    "finding",
    "group",
    "untested",
    "classified_status",
    "classified_finished_at",
    "source",
    "corrected_by_label",
    "corrected_at",
    "updated_at",
)


@dataclass(frozen=True)
class CaseCorrection:
    ref: str
    category: str | None
    severity: str | None
    finding: str | None
    group: str | None


@dataclass(frozen=True)
class _Target:
    pivot_id: int
    ref: str
    status: str
    finished_at: datetime | None


def correct_triage_cases(
    *,
    test_run_id: int,
    turn_id: int,
    changes: tuple[CaseCorrection, ...],
    reason: str,
    corrected_by_label: str,
) -> str:
    """Apply a person's corrections to cases of the run; the tool result text."""
    if not changes:
        raise RunTriageError("Give at least one change.")
    if len(changes) > MAX_CHANGES_PER_CALL:
        raise RunTriageError(
            f"At most {MAX_CHANGES_PER_CALL} changes per call; split the rest."
        )
    why = cut(one_line(reason), _REASON_MAX)
    if not why:
        raise RunTriageError("Say why (the person's request) in reason.")
    targets = _targets(test_run_id, [change.ref for change in changes])
    cleaned = [_clean(change) for change in changes]
    label = cut(one_line(corrected_by_label), _LABEL_MAX)
    now = timezone.now()

    with transaction.atomic():
        require_running_turn(turn_id)
        triage, _ = RunTriage.objects.get_or_create(test_run_id=test_run_id)
        existing = {
            obj.test_run_test_case_id: obj
            for obj in TriageCase.objects.filter(
                triage=triage,
                test_run_test_case_id__in=[t.pivot_id for t in targets],
            ).only("id", "test_run_test_case_id", *_ROW_FIELDS)
        }
        missing = [
            target.ref
            for target, change in zip(targets, cleaned)
            if target.pivot_id not in existing
            and not (change.category and change.severity and change.finding)
        ]
        if missing:
            raise RunTriageError(
                "Not classified yet, so give category, severity and finding for: "
                + ", ".join(missing[:20])
            )
        groups = _GroupFinder(triage.id)
        groups.resolve(
            (change.category or existing[target.pivot_id].category, change.group)
            for target, change in zip(targets, cleaned)
            if change.group
        )
        updated: list[TriageCase] = []
        created: list[TriageCase] = []
        lines: list[str] = []
        for target, change in zip(targets, cleaned):
            obj = existing.get(target.pivot_id)
            if obj is None:
                obj = TriageCase(triage=triage, test_run_test_case_id=target.pivot_id)
                created.append(obj)
            else:
                updated.append(obj)
            if change.category and change.category != obj.category:
                obj.category = change.category
                obj.group = None
            if change.severity:
                obj.severity = change.severity
            if change.finding:
                obj.finding = change.finding
            if change.group:
                obj.group = groups.get(obj.category, change.group)
            obj.untested = is_untested(target.status)
            obj.classified_status = target.status
            obj.classified_finished_at = target.finished_at
            obj.source = str(TriageSource.PERSON)
            obj.corrected_by_label = label
            obj.corrected_at = now
            obj.updated_at = now
            group_name = groups.title_of(obj.group_id)
            group_text = f", group {group_name!r}" if group_name else ""
            lines.append(
                f"- {target.ref}: {category_label(obj.category)} / "
                f"{severity_label(obj.severity)}{group_text}"
            )
        if updated:
            TriageCase.objects.bulk_update(updated, list(_ROW_FIELDS))
        if created:
            TriageCase.objects.bulk_create(created)
        RunTriage.objects.filter(id=triage.id).update(updated_at=now)

    return "\n".join(
        [
            f"Updated {plural(len(targets), 'case')}, marked as corrected by "
            f"{label or 'a person'} (reason: {why}).",
            *lines,
        ]
    )


def record_triage_groups(
    *,
    test_run_id: int,
    turn_id: int,
    category: str,
    note: str | None,
    groups: tuple[GroupResult, ...],
    corrected_by_label: str,
) -> str:
    """Replace a category's grouping (and note) as the person asked; every
    case of the category must appear exactly once. The groups become a
    person's, so AI grouping keeps them."""
    if category not in CATEGORY_ORDER:
        raise RunTriageError(
            f"Unknown category {category!r}; use one of: " + ", ".join(CATEGORY_ORDER)
        )
    if len(groups) > MAX_GROUPS_PER_CATEGORY:
        raise RunTriageError(f"At most {MAX_GROUPS_PER_CATEGORY} groups per category.")
    if note is not None and len(note) > TRIAGE_NOTE_MAX_LENGTH:
        raise RunTriageError(
            f"The note is longer than {TRIAGE_NOTE_MAX_LENGTH} characters."
        )
    triage_id = (
        RunTriage.objects.filter(test_run_id=test_run_id)
        .values_list("id", flat=True)
        .first()
    )
    if triage_id is None:
        raise RunTriageError("This run has no case analysis yet.")
    ref_map = run_ref_map(test_run_id)
    row_ids = dict(
        TriageCase.objects.filter(triage_id=triage_id, category=category).values_list(
            "test_run_test_case_id", "id"
        )
    )
    planned = _plan_groups(groups, ref_map, row_ids, category)

    with transaction.atomic():
        require_running_turn(turn_id)
        # Same check under the lock: the rows may have changed meanwhile.
        current = set(
            TriageCase.objects.filter(
                triage_id=triage_id, category=category
            ).values_list("id", flat=True)
        )
        if current != set(row_ids.values()):
            raise RunTriageError(
                "The category's cases changed meanwhile; read them again with "
                "list_triage_cases."
            )
        TriageGroup.objects.filter(triage_id=triage_id, category=category).delete()
        create_groups(
            triage_id=triage_id,
            category=category,
            groups=planned,
            source=str(TriageSource.PERSON),
            start_position=0,
        )
        write_note(triage_id, category, note, grouped=True)

    label = cut(one_line(corrected_by_label), _LABEL_MAX) or "a person"
    return (
        f"Recorded {plural(len(planned), 'group')} for {category_label(category)} "
        f"as asked by {label}.\n" + triage_outline_text(test_run_id, [category])
    )


def _targets(test_run_id: int, refs: Sequence[str]) -> list[_Target]:
    """The analysable cases ``refs`` name (two queries); errors list bad refs."""
    ref_map = run_ref_map(test_run_id)
    pivot_ids: list[int] = []
    unknown: list[str] = []
    seen: set[int] = set()
    duplicated: list[str] = []
    for ref in refs:
        pivot_id = ref_map.get(normalize_ref(ref))
        if pivot_id is None:
            unknown.append(ref)
        elif pivot_id in seen:
            duplicated.append(ref)
        else:
            seen.add(pivot_id)
            pivot_ids.append(pivot_id)
    if unknown:
        raise RunTriageError("Not cases of this run: " + ", ".join(unknown[:20]))
    if duplicated:
        raise RunTriageError(
            "Each case may appear once per call: " + ", ".join(duplicated[:20])
        )
    states = {
        pivot_id: (status, finished_at)
        for pivot_id, status, finished_at in TestRunTestCase.objects.filter(
            analysable_q(), test_run_id=test_run_id, id__in=pivot_ids
        ).values_list("id", "status", "finished_at")
    }
    not_analysable = [
        ref for ref, pivot_id in zip(refs, pivot_ids) if pivot_id not in states
    ]
    if not_analysable:
        raise RunTriageError(
            "Only analysed cases (Failed, Blocked, Needs re-test, Cancelled with a "
            "result) can be corrected: " + ", ".join(not_analysable[:20])
        )
    return [
        _Target(
            pivot_id=pivot_id,
            ref=normalize_ref(ref),
            status=states[pivot_id][0],
            finished_at=states[pivot_id][1],
        )
        for ref, pivot_id in zip(refs, pivot_ids)
    ]


def _clean(change: CaseCorrection) -> CaseCorrection:
    """Validated values ("" for a field not changed); ``RunTriageError`` otherwise."""
    category = (change.category or "").strip()
    severity = (change.severity or "").strip()
    finding = cut(one_line(change.finding or ""), TRIAGE_FINDING_MAX_LENGTH)
    group = group_title(change.group or "")
    if category and category not in CATEGORY_ORDER:
        raise RunTriageError(
            f"{change.ref}: unknown category {category!r}; use one of: "
            + ", ".join(CATEGORY_ORDER)
        )
    if severity and severity not in SEVERITY_ORDER:
        raise RunTriageError(
            f"{change.ref}: unknown severity {severity!r}; use one of: "
            + ", ".join(SEVERITY_ORDER)
        )
    if not (category or severity or finding or group):
        raise RunTriageError(f"{change.ref}: nothing to change.")
    return CaseCorrection(
        ref=change.ref,
        category=category,
        severity=severity,
        finding=finding,
        group=group,
    )


class _GroupFinder:
    """Groups of the triage by category and title, loaded once. A title not
    found creates a person group (one bulk insert); a matched AI group becomes
    a person's (one update), so AI grouping keeps the corrected case in it."""

    def __init__(self, triage_id: int) -> None:
        self._triage_id = triage_id
        self._groups: list[TriageGroup] = []
        self._by_key: dict[tuple[str, str], TriageGroup] = {}

    def resolve(self, wanted: Iterable[tuple[str, str]]) -> None:
        """Find or create every (category, title) group of this call."""
        requests = list(dict.fromkeys((c, t.casefold(), t) for c, t in wanted))
        self._groups = list(TriageGroup.objects.filter(triage_id=self._triage_id))
        found: dict[tuple[str, str], TriageGroup] = {}
        for group in self._groups:
            found.setdefault((group.category, group.title.casefold()), group)
        claim: list[TriageGroup] = []
        positions: dict[str, int] = {}
        for group in self._groups:
            if group.parent_id is None:
                positions[group.category] = max(
                    positions.get(group.category, -1), group.position
                )
        new: list[TriageGroup] = []
        for category, key, title in requests:
            if (category, key) in self._by_key:
                continue
            match = found.get((category, key))
            if match is not None:
                group = match
                claim.append(group)
                if group.parent_id is not None:
                    claim.extend(g for g in self._groups if g.id == group.parent_id)
            else:
                position = positions.get(category, -1) + 1
                positions[category] = position
                group = TriageGroup(
                    triage_id=self._triage_id,
                    category=category,
                    title=title,
                    position=position,
                    source=TriageSource.PERSON,
                )
                new.append(group)
            self._by_key[(category, key)] = group
        to_claim = [g for g in claim if g.source != TriageSource.PERSON]
        if to_claim:
            TriageGroup.objects.filter(id__in=[g.id for g in to_claim]).update(
                source=TriageSource.PERSON
            )
            for group in to_claim:
                group.source = str(TriageSource.PERSON)
        if new:
            TriageGroup.objects.bulk_create(new)
            self._groups.extend(new)

    def get(self, category: str, title: str) -> TriageGroup:
        return self._by_key[(category, title.casefold())]

    def title_of(self, group_id: int | None) -> str:
        if group_id is None:
            return ""
        if not self._groups:
            self._groups = list(TriageGroup.objects.filter(triage_id=self._triage_id))
        return next((g.title for g in self._groups if g.id == group_id), "")


def _plan_groups(
    groups: Sequence[GroupResult],
    ref_map: dict[str, int],
    row_ids: dict[int, int],
    category: str,
) -> list[PlannedGroup]:
    """Validate that each case of the category appears exactly once."""
    seen: set[int] = set()
    unknown: list[str] = []
    foreign: list[str] = []
    duplicated: list[str] = []

    def take(refs: Sequence[str]) -> tuple[int, ...]:
        ids: list[int] = []
        for ref in refs:
            pivot_id = ref_map.get(normalize_ref(ref))
            if pivot_id is None:
                unknown.append(ref)
            elif pivot_id not in row_ids:
                foreign.append(ref)
            elif pivot_id in seen:
                duplicated.append(ref)
            else:
                seen.add(pivot_id)
                ids.append(row_ids[pivot_id])
        return tuple(ids)

    planned: list[PlannedGroup] = []
    for group in groups:
        if not group.title.strip():
            raise RunTriageError("Every group needs a title (its finding).")
        direct = take(group.refs)
        subs: list[PlannedSubGroup] = []
        for sub in group.sub_groups:
            if not sub.title.strip():
                raise RunTriageError("Every sub-group needs a title.")
            ids = take(sub.refs)
            if ids:
                subs.append(PlannedSubGroup(title=sub.title, row_ids=ids))
        if direct or subs:
            planned.append(
                PlannedGroup(title=group.title, row_ids=direct, sub_groups=tuple(subs))
            )
    problems: list[str] = []
    if unknown:
        problems.append("not cases of this run: " + ", ".join(unknown[:20]))
    if foreign:
        problems.append(
            f"not in {category_label(category)}: " + ", ".join(foreign[:20])
        )
    if duplicated:
        problems.append("listed more than once: " + ", ".join(duplicated[:20]))
    missing = [pivot_id for pivot_id in row_ids if pivot_id not in seen]
    if missing:
        refs_by_pivot = {pid: ref for ref, pid in ref_map.items() if ref[0] == "T"}
        names = [refs_by_pivot.get(pid, f"R{pid}") for pid in missing[:20]]
        more = f" and {len(missing) - 20} more" if len(missing) > 20 else ""
        problems.append("missing: " + ", ".join(names) + more)
    if problems:
        raise RunTriageError(
            "Every case of the category must appear exactly once; "
            + "; ".join(problems)
        )
    return planned
