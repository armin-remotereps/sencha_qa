"""A person's decision on a run proposal: create its runs as Drafts, or discard it."""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from machine_setup.exceptions import RunProposalClosedError, RunProposalEmptyError
from machine_setup.models import SetupRunProposal, SetupRunProposalStatus
from machine_setup.services.run_proposal_events import broadcast_proposal_card
from projects.models import Project, TestCase
from projects.services import create_test_run_with_cases

_CLOSED_MESSAGES: dict[str, str] = {
    SetupRunProposalStatus.CREATED: "These runs were already created.",
    SetupRunProposalStatus.SUPERSEDED: "This proposal was replaced by a newer one.",
    SetupRunProposalStatus.DISCARDED: "This proposal was discarded.",
}


@dataclass(frozen=True)
class CreatedRunsOutcome:
    created_count: int
    # Names of runs left out because none of their cases exist any more.
    skipped_names: list[str]


@transaction.atomic
def create_proposed_runs(
    *, project: Project, proposal_id: int, user: CustomUser
) -> CreatedRunsOutcome:
    """Create every run of a pending proposal as a Draft.

    The proposal row is locked, so a double submit sees ``created`` and gets
    ``RunProposalClosedError``. Cases deleted since the proposal are dropped
    (one query); a run left empty is skipped, and if every run is, nothing is
    created and the proposal stays pending. Each run goes through
    ``create_test_run_with_cases`` (about three queries per run).
    """
    proposal = _locked_open_proposal(
        project=project, proposal_id=proposal_id, with_case_ids=True
    )
    names = [_run_name(row) for row in _list(proposal.runs)]
    id_lists = [_int_list(ids) for ids in _list(proposal.case_ids)]
    existing = set(
        TestCase.objects.filter(
            project=project, id__in={i for ids in id_lists for i in ids}
        ).values_list("id", flat=True)
    )
    created: list[dict[str, object]] = []
    skipped: list[str] = []
    for name, ids in zip(names, id_lists):
        kept = [i for i in ids if i in existing]
        if not kept:
            skipped.append(name)
            continue
        run = create_test_run_with_cases(project=project, test_case_ids=kept, name=name)
        created.append({"run_id": run.pk, "name": name, "case_count": len(kept)})
    if not created:
        raise RunProposalEmptyError()
    proposal.status = SetupRunProposalStatus.CREATED
    proposal.created_runs = created
    proposal.skipped = skipped
    proposal.decided_by = user
    proposal.decided_at = timezone.now()
    proposal.save(
        update_fields=["status", "created_runs", "skipped", "decided_by", "decided_at"]
    )
    transaction.on_commit(lambda: broadcast_proposal_card(proposal_id=proposal.pk))
    return CreatedRunsOutcome(created_count=len(created), skipped_names=skipped)


@transaction.atomic
def discard_run_proposal(
    *, project: Project, proposal_id: int, user: CustomUser
) -> None:
    """Discard a pending proposal; nothing is created."""
    proposal = _locked_open_proposal(
        project=project, proposal_id=proposal_id, with_case_ids=False
    )
    proposal.status = SetupRunProposalStatus.DISCARDED
    proposal.decided_by = user
    proposal.decided_at = timezone.now()
    proposal.save(update_fields=["status", "decided_by", "decided_at"])
    transaction.on_commit(lambda: broadcast_proposal_card(proposal_id=proposal.pk))


def _locked_open_proposal(
    *, project: Project, proposal_id: int, with_case_ids: bool
) -> SetupRunProposal:
    """The proposal, locked, if it is in ``project``, pending and its chat live.

    Never loads the chat's ``llm_transcript``; ``case_ids`` only on request.
    """
    deferred = ["conversation__llm_transcript"]
    if not with_case_ids:
        deferred.append("case_ids")
    proposal = (
        SetupRunProposal.objects.select_for_update(of=("self",))
        .select_related("conversation")
        .defer(*deferred)
        .filter(pk=proposal_id, project=project)
        .first()
    )
    if proposal is None:
        raise RunProposalClosedError("This proposal was not found.")
    closed = _CLOSED_MESSAGES.get(proposal.status)
    if closed is not None:
        raise RunProposalClosedError(closed)
    if proposal.conversation.archived_at is not None:
        raise RunProposalClosedError("The chat was reset.")
    return proposal


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _run_name(row: object) -> str:
    name = row.get("name", "") if isinstance(row, dict) else ""
    return str(name)


def _int_list(value: object) -> list[int]:
    return [i for i in _list(value) if isinstance(i, int) and not isinstance(i, bool)]
