"""A person's decision on a result-chat card: apply or discard a result change,
re-run the case with a redesigned plan, or discard the redesign.

``find_result_change_card`` / ``find_redesign_card`` scope the id to the
project (None is the view's 404). The decisions call the ``projects``
services, which lock, re-check and raise their own domain errors
(``ResultChangeError``, ``PlanRevisionError``, ``RunResumeError``), then
re-broadcast the case's cards after commit.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from accounts.models import CustomUser
from machine_setup.models import SetupConversation
from machine_setup.services.result_card_events import broadcast_case_cards
from projects.case_result_change_services import (
    apply_result_change,
    discard_result_change,
)
from projects.case_review_redesign_services import (
    discard_review_plan_revision,
    rerun_with_redesigned_plan,
)
from projects.models import (
    CasePlanRevision,
    CaseResultChange,
    PlanRevisionSource,
    Project,
    TestRunTestCase,
)


@dataclass(frozen=True)
class ResultCard:
    """A card's proposal row, scoped to the project, and where to go back to."""

    item_id: int
    pivot: TestRunTestCase
    # The card's result chat when it is still open; None sends the person
    # to the case page.
    conversation_id: int | None
    # Redesigns: the first step that runs on the re-run; 0 for result changes.
    resume_step: int


def find_result_change_card(project: Project, change_id: int) -> ResultCard | None:
    """The result change of this project, or None; two queries."""
    change = (
        CaseResultChange.objects.filter(
            pk=change_id, test_run_test_case__test_run__project=project
        )
        .select_related("test_run_test_case")
        # The decision services read only the case's id and run id.
        .only(
            "id",
            "conversation_id",
            "test_run_test_case__id",
            "test_run_test_case__test_run_id",
        )
        .first()
    )
    if change is None:
        return None
    return ResultCard(
        item_id=change.pk,
        pivot=change.test_run_test_case,
        conversation_id=_open_thread_id(project, change.conversation_id),
        resume_step=0,
    )


def find_redesign_card(project: Project, revision_id: int) -> ResultCard | None:
    """The result-chat (review) redesign of this project, or None; two queries.

    A help-chat redesign is not found here: it belongs to the help panel.
    """
    revision = (
        CasePlanRevision.objects.filter(
            pk=revision_id,
            source=PlanRevisionSource.REVIEW,
            test_run_test_case__test_run__project=project,
        )
        .select_related("test_run_test_case")
        # The decision services read only the case's id and run id.
        .only(
            "id",
            "conversation_id",
            "resume_step",
            "test_run_test_case__id",
            "test_run_test_case__test_run_id",
        )
        .first()
    )
    if revision is None:
        return None
    return ResultCard(
        item_id=revision.pk,
        pivot=revision.test_run_test_case,
        conversation_id=_open_thread_id(project, revision.conversation_id),
        resume_step=revision.resume_step,
    )


def apply_result_change_from_chat(*, card: ResultCard, user: CustomUser) -> None:
    apply_result_change(pivot=card.pivot, change_id=card.item_id, user=user)
    _rebroadcast(card, change_id=card.item_id)


def discard_result_change_from_chat(*, card: ResultCard, user: CustomUser) -> None:
    discard_result_change(pivot=card.pivot, change_id=card.item_id, user=user)
    _rebroadcast(card, change_id=card.item_id)


def rerun_redesign_from_chat(
    *, card: ResultCard, user: CustomUser, help_mode: str | None
) -> None:
    rerun_with_redesigned_plan(
        pivot=card.pivot, revision_id=card.item_id, user=user, help_mode=help_mode
    )
    _rebroadcast(card, revision_id=card.item_id)


def discard_redesign_from_chat(*, card: ResultCard, user: CustomUser) -> None:
    discard_review_plan_revision(pivot=card.pivot, revision_id=card.item_id, user=user)
    _rebroadcast(card, revision_id=card.item_id)


def _rebroadcast(
    card: ResultCard, *, change_id: int | None = None, revision_id: int | None = None
) -> None:
    pivot_id = card.pivot.pk
    transaction.on_commit(
        lambda: broadcast_case_cards(
            pivot_id=pivot_id,
            decided_change_id=change_id,
            decided_revision_id=revision_id,
        )
    )


def _open_thread_id(project: Project, conversation_id: int | None) -> int | None:
    if conversation_id is None:
        return None
    exists = SetupConversation.objects.filter(
        pk=conversation_id, project=project, archived_at__isnull=True
    ).exists()
    return conversation_id if exists else None
