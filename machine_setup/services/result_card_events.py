"""Live updates of a finished case's result-chat cards (result changes and redesigns)."""

from __future__ import annotations

import logging

from django.db.models import Q, Subquery

from machine_setup.events import broadcast_message
from machine_setup.models import SetupMessage, SetupMessageKind
from machine_setup.payloads import CARD_DEFER, CARD_SELECT_RELATED, message_payload
from projects.models import (
    CasePlanRevision,
    CaseResultChange,
    PlanRevisionStatus,
    ResultChangeStatus,
)

logger = logging.getLogger(__name__)


def broadcast_case_cards(
    *,
    pivot_id: int,
    decided_change_id: int | None = None,
    decided_revision_id: int | None = None,
) -> None:
    """Re-broadcast the case's live card rows of its result chats; one query.

    A decision or a newer proposal changes the state (status, outdated) of
    the case's pending cards too, so every row whose change or redesign is
    still proposed is sent, plus the rows of the change or redesign just
    decided and of the newest replaced ones. Older decided cards cannot change
    and are not re-sent. Never raises: an open page only misses a live update
    and shows the state on its next load.
    """
    try:
        rows = (
            SetupMessage.objects.filter(
                _live_card_rows(pivot_id, decided_change_id, decided_revision_id),
                kind=SetupMessageKind.NOTICE,
            )
            .select_related("conversation", "author", *CARD_SELECT_RELATED)
            .defer("conversation__llm_transcript", *CARD_DEFER)
        )
        for row in rows:
            broadcast_message(
                row.conversation.project_id,
                message_payload(
                    row,
                    screenshots=[],
                    attachments=[],
                    project_id=row.conversation.project_id,
                ),
            )
    except Exception:
        logger.exception("Result chat cards of case %s: broadcast failed", pivot_id)


def _live_card_rows(
    pivot_id: int, decided_change_id: int | None, decided_revision_id: int | None
) -> Q:
    """Card rows of the case whose state may have just changed.

    Pending proposals, the decided ones, and the newest replaced change and
    redesign (a new proposal, or a re-run, retires the pending one).
    """
    replaced_change = Subquery(
        CaseResultChange.objects.filter(
            test_run_test_case_id=pivot_id, status=ResultChangeStatus.SUPERSEDED
        )
        .order_by("-decided_at", "-id")
        .values("id")[:1]
    )
    replaced_revision = Subquery(
        CasePlanRevision.objects.filter(
            test_run_test_case_id=pivot_id, status=PlanRevisionStatus.SUPERSEDED
        )
        .order_by("-decided_at", "-id")
        .values("id")[:1]
    )
    rows = Q(
        result_change__test_run_test_case_id=pivot_id,
        result_change__status=ResultChangeStatus.PROPOSED,
    ) | Q(
        plan_revision__test_run_test_case_id=pivot_id,
        plan_revision__status=PlanRevisionStatus.PROPOSED,
    )
    rows |= Q(result_change_id=replaced_change) | Q(plan_revision_id=replaced_revision)
    if decided_change_id is not None:
        rows |= Q(
            result_change_id=decided_change_id,
            result_change__test_run_test_case_id=pivot_id,
        )
    if decided_revision_id is not None:
        rows |= Q(
            plan_revision_id=decided_revision_id,
            plan_revision__test_run_test_case_id=pivot_id,
        )
    return rows
