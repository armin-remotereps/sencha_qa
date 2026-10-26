"""The main agent proposes a set of test runs; a person creates them later.

Nothing is created here: the proposal is stored, the turn runner posts the
card, and ``run_proposal_decision.create_proposed_runs`` runs on the person's
click.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial

from django.db import IntegrityError, transaction
from django.db.models.functions import Lower

from machine_setup.exceptions import RunProposalInvalidError
from machine_setup.models import (
    SetupConversation,
    SetupConversationKind,
    SetupRunProposal,
    SetupRunProposalStatus,
)
from machine_setup.run_proposal_types import (
    MAX_CASES_PER_PROPOSED_RUN,
    MAX_PROPOSED_RUNS,
    PROPOSAL_NOTE_MAX,
    PROPOSE_TEST_RUNS_TOOL,
    RUN_NAME_MAX,
    ResolvedSelection,
    RunSpec,
)
from machine_setup.services.run_case_selection import resolve_selection
from machine_setup.services.run_proposal_events import broadcast_proposal_card
from projects.models import TestRun


@dataclass(frozen=True)
class ProposalOutcome:
    proposal_id: int
    run_count: int
    tool_text: str


def propose_test_runs(
    *,
    project_id: int,
    conversation_id: int,
    turn_id: int | None,
    specs: list[RunSpec],
    note: str,
) -> ProposalOutcome:
    """Validate and store a proposal, replacing the chat's pending one.

    One query per run resolves its cases and one flags names already used by
    the project's runs; the store runs under a lock on the conversation row.
    Raises ``RunProposalInvalidError`` with a message the agent can act on.
    """
    names = _validated_names(specs)
    note = note.strip()
    if len(note) > PROPOSAL_NOTE_MAX:
        raise RunProposalInvalidError(
            f"The note is {len(note)} characters; keep it to {PROPOSAL_NOTE_MAX}."
        )
    selections = [resolve_selection(project_id=project_id, spec=spec) for spec in specs]
    _check_sizes(names, selections)
    used = _names_in_use(project_id, names)
    runs = [
        {
            "name": name,
            "case_count": len(selection.case_ids),
            "sample_titles": selection.sample_titles,
            "name_exists": name.lower() in used,
        }
        for name, selection in zip(names, selections)
    ]
    try:
        with transaction.atomic():
            conversation = (
                SetupConversation.objects.select_for_update()
                .filter(pk=conversation_id, project_id=project_id)
                .only("id", "archived_at", "kind")
                .first()
            )
            if conversation is None or conversation.archived_at is not None:
                raise RunProposalInvalidError(
                    "This chat was reset; the proposal was not stored."
                )
            if conversation.kind != SetupConversationKind.CHAT:
                raise RunProposalInvalidError(
                    "Test runs can be proposed in the main Machine setup chat only."
                )
            pending = SetupRunProposal.objects.filter(
                conversation_id=conversation_id,
                status=SetupRunProposalStatus.PENDING,
            )
            superseded_ids = list(pending.values_list("id", flat=True))
            if superseded_ids:
                pending.update(status=SetupRunProposalStatus.SUPERSEDED)
            proposal = SetupRunProposal.objects.create(
                project_id=project_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                status=SetupRunProposalStatus.PENDING,
                note=note,
                runs=runs,
                case_ids=[selection.case_ids for selection in selections],
            )
            for superseded_id in superseded_ids:
                transaction.on_commit(
                    partial(broadcast_proposal_card, proposal_id=superseded_id)
                )
    except IntegrityError as exc:
        raise RunProposalInvalidError(
            "Another proposal was stored at the same time. Call "
            f"{PROPOSE_TEST_RUNS_TOOL} once with the whole set of runs."
        ) from exc
    return ProposalOutcome(
        proposal_id=proposal.pk,
        run_count=len(runs),
        tool_text=_tool_text(runs, replaced=bool(superseded_ids)),
    )


def _validated_names(specs: list[RunSpec]) -> list[str]:
    """Trimmed run names; refuses a bad count, blank, long or duplicate names."""
    if not specs:
        raise RunProposalInvalidError("Propose at least one run.")
    if len(specs) > MAX_PROPOSED_RUNS:
        raise RunProposalInvalidError(
            f"{len(specs)} runs proposed; at most {MAX_PROPOSED_RUNS} per proposal."
        )
    names: list[str] = []
    seen: set[str] = set()
    duplicates: list[str] = []
    for index, spec in enumerate(specs, start=1):
        name = spec.name.strip()
        if not name:
            raise RunProposalInvalidError(f"Run {index} has no name.")
        if len(name) > RUN_NAME_MAX:
            raise RunProposalInvalidError(
                f"Run {index} has a name over {RUN_NAME_MAX} characters."
            )
        if not spec.has_selector():
            raise RunProposalInvalidError(
                f"Run {name!r} selects no cases: set all_cases, a filter, "
                "case_ids or testrail_case_ids."
            )
        key = name.lower()
        if key in seen and name not in duplicates:
            duplicates.append(name)
        seen.add(key)
        names.append(name)
    if duplicates:
        listed = ", ".join(repr(name) for name in duplicates)
        raise RunProposalInvalidError(
            f"Run names must be unique within a proposal; repeated: {listed}."
        )
    return names


def _check_sizes(names: list[str], selections: list[ResolvedSelection]) -> None:
    empty = [n for n, s in zip(names, selections) if not s.case_ids]
    too_big = [
        n
        for n, s in zip(names, selections)
        if len(s.case_ids) > MAX_CASES_PER_PROPOSED_RUN
    ]
    problems: list[str] = []
    if empty:
        problems.append(
            "These runs match no case of this project: "
            + ", ".join(repr(n) for n in empty)
            + "."
        )
    if too_big:
        problems.append(
            f"These runs match more than {MAX_CASES_PER_PROPOSED_RUN} cases; "
            "narrow or split them: " + ", ".join(repr(n) for n in too_big) + "."
        )
    if problems:
        raise RunProposalInvalidError(" ".join(problems) + " Nothing was stored.")


def _names_in_use(project_id: int, names: list[str]) -> set[str]:
    """Lower-cased proposed names that an existing run of the project has."""
    return set(
        TestRun.objects.filter(project_id=project_id)
        .annotate(lower_name=Lower("name"))
        .filter(lower_name__in=[name.lower() for name in names])
        .values_list("lower_name", flat=True)
    )


def _tool_text(runs: list[dict[str, object]], *, replaced: bool) -> str:
    lines = [f"Proposed {len(runs)} test run{'s' if len(runs) != 1 else ''}:"]
    for run in runs:
        warning = (
            " Warning: name already used by an existing run."
            if run["name_exists"]
            else ""
        )
        lines.append(f"- {run['name']!r}: {run['case_count']} cases.{warning}")
    if replaced:
        lines.append("This replaces the earlier pending proposal in this chat.")
    lines.append(
        "Nothing is created yet: the person reviews the card in the chat and "
        "presses Create runs."
    )
    return "\n".join(lines)
