"""Read a user's attention inbox: missed failures and missed setup replies,
plus the person steps waiting for a project member (Human in the loop).

Rows are filtered at read time, so nothing has to clean them up:

- a missed failure is listed only while its run case is still Failed (a re-run
  or a changed result hides it; failing again brings it back);
- a missed reply is listed only while its thread is not archived;
- both only for projects in ``project_ids`` (the caller passes the user's
  non-archived member projects, ``dashboard.services.member_projects``).

Every query is scoped to ``user`` and bounded by ``limit``. Person steps are
not per-user rows: an open ``CasePersonStep`` (``waiting`` or
``not_confirmed``) is listed for every member of its project, read live like
the open help requests, so it disappears as soon as someone reports it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.db.models import QuerySet
from django.db.models.functions import Substr
from django.urls import reverse

from accounts.models import CustomUser
from machine_setup.services.conversation_url import conversation_path
from notifications.models import MissedFailure, MissedReply, MissedReplyOutcome
from projects.models import CasePersonStep, PersonStepStatus, TestRunTestCaseStatus

# The inbox shows the start of the verdict only; the case page has the rest.
VERDICT_PREVIEW_LENGTH = 200


@dataclass(frozen=True)
class InboxFailure:
    """A case the agent judged Failed that the user has not dismissed."""

    id: int
    pivot_id: int
    case_title: str
    testrail_id: str
    project_id: int
    project_name: str
    run_id: int
    run_name: str
    # At most VERDICT_PREVIEW_LENGTH characters of the model-written verdict
    # (render escaped).
    verdict: str
    occurred_at: datetime
    url: str


@dataclass(frozen=True)
class InboxReply:
    """A setup turn that ended while its sender was away, not yet dismissed."""

    id: int
    conversation_id: int
    conversation_label: str
    project_id: int
    project_name: str
    outcome: MissedReplyOutcome
    outcome_label: str
    # Model-written plain text (render escaped).
    preview: str
    occurred_at: datetime
    url: str


@dataclass(frozen=True)
class InboxPersonStep:
    """A step of a Human in the loop run waiting for a person ("Your turn")."""

    id: int
    pivot_id: int
    case_title: str
    testrail_id: str
    project_name: str
    run_name: str
    step_num: int
    # At most VERDICT_PREVIEW_LENGTH characters of the model-written
    # instruction (render escaped).
    instruction: str
    # The agent could not confirm the last report.
    not_confirmed: bool
    requested_at: datetime
    url: str


# The person's turn: the agent is not working on these.
_WAITING_PERSON_STEP_STATUSES = (
    PersonStepStatus.WAITING,
    PersonStepStatus.NOT_CONFIRMED,
)


@dataclass(frozen=True)
class Inbox:
    """The newest ``limit`` items of each kind plus the full open totals."""

    failures: list[InboxFailure]
    replies: list[InboxReply]
    failure_total: int
    reply_total: int


def _open_failures(user: CustomUser, project_ids: list[int]) -> QuerySet[MissedFailure]:
    return MissedFailure.objects.filter(
        user=user,
        dismissed_at__isnull=True,
        test_run_test_case__status=TestRunTestCaseStatus.FAILED,
        test_run_test_case__test_run__project_id__in=project_ids,
    )


def _open_replies(user: CustomUser, project_ids: list[int]) -> QuerySet[MissedReply]:
    return MissedReply.objects.filter(
        user=user,
        dismissed_at__isnull=True,
        conversation__archived_at__isnull=True,
        conversation__project_id__in=project_ids,
    )


def open_person_steps(project_ids: list[int]) -> QuerySet[CasePersonStep]:
    """Person steps waiting for a member of ``project_ids`` (not user-scoped)."""
    return CasePersonStep.objects.filter(
        status__in=_WAITING_PERSON_STEP_STATUSES,
        test_run_test_case__test_run__project_id__in=project_ids,
    )


def person_steps(project_ids: list[int], limit: int) -> list[InboxPersonStep]:
    """The newest ``limit`` waiting person steps, oldest wait last (one query)."""
    if not project_ids:
        return []
    rows = (
        open_person_steps(project_ids)
        .order_by("-requested_at", "-id")
        .annotate(instruction_head=Substr("instruction", 1, VERDICT_PREVIEW_LENGTH))
        .values_list(
            "id",
            "step_num",
            "instruction_head",
            "status",
            "requested_at",
            "test_run_test_case_id",
            "test_run_test_case__test_case__title",
            "test_run_test_case__test_case__testrail_id",
            "test_run_test_case__test_run__project_id",
            "test_run_test_case__test_run__project__name",
            "test_run_test_case__test_run_id",
            "test_run_test_case__test_run__name",
        )[:limit]
    )
    items: list[InboxPersonStep] = []
    for (
        item_id,
        step_num,
        instruction,
        status,
        requested_at,
        pivot_id,
        case_title,
        testrail_id,
        project_id,
        project_name,
        run_id,
        run_name,
    ) in rows:
        items.append(
            InboxPersonStep(
                id=item_id,
                pivot_id=pivot_id,
                case_title=case_title,
                testrail_id=testrail_id,
                project_name=project_name,
                # Same fallback as TestRun.display_name.
                run_name=run_name or f"Run #{run_id}",
                step_num=step_num,
                instruction=instruction or "",
                not_confirmed=status == PersonStepStatus.NOT_CONFIRMED,
                requested_at=requested_at,
                url=reverse(
                    "projects:test_run_case_detail",
                    kwargs={
                        "project_id": project_id,
                        "test_run_id": run_id,
                        "pivot_id": pivot_id,
                    },
                ),
            )
        )
    return items


def _failures(
    user: CustomUser, project_ids: list[int], limit: int
) -> list[InboxFailure]:
    rows = (
        _open_failures(user, project_ids)
        .order_by("-occurred_at", "-id")
        .annotate(
            verdict=Substr("test_run_test_case__result", 1, VERDICT_PREVIEW_LENGTH)
        )
        .values_list(
            "id",
            "test_run_test_case_id",
            "test_run_test_case__test_case__title",
            "test_run_test_case__test_case__testrail_id",
            "test_run_test_case__test_run__project_id",
            "test_run_test_case__test_run__project__name",
            "test_run_test_case__test_run_id",
            "test_run_test_case__test_run__name",
            "verdict",
            "occurred_at",
        )[:limit]
    )
    items: list[InboxFailure] = []
    for (
        item_id,
        pivot_id,
        case_title,
        testrail_id,
        project_id,
        project_name,
        run_id,
        run_name,
        verdict,
        occurred_at,
    ) in rows:
        items.append(
            InboxFailure(
                id=item_id,
                pivot_id=pivot_id,
                case_title=case_title,
                testrail_id=testrail_id,
                project_id=project_id,
                project_name=project_name,
                run_id=run_id,
                # Same fallback as TestRun.display_name.
                run_name=run_name or f"Run #{run_id}",
                verdict=verdict or "",
                occurred_at=occurred_at,
                url=reverse(
                    "projects:test_run_case_detail",
                    kwargs={
                        "project_id": project_id,
                        "test_run_id": run_id,
                        "pivot_id": pivot_id,
                    },
                ),
            )
        )
    return items


def _replies(user: CustomUser, project_ids: list[int], limit: int) -> list[InboxReply]:
    rows = (
        _open_replies(user, project_ids)
        .select_related("conversation__project")
        .only(
            "id",
            "outcome",
            "preview",
            "occurred_at",
            "conversation__id",
            "conversation__kind",
            "conversation__project__id",
            "conversation__project__name",
        )
        .order_by("-occurred_at", "-id")[:limit]
    )
    return [
        InboxReply(
            id=row.pk,
            conversation_id=row.conversation.pk,
            conversation_label=row.conversation.get_kind_display(),
            project_id=row.conversation.project.pk,
            project_name=row.conversation.project.name,
            outcome=MissedReplyOutcome(row.outcome),
            outcome_label=row.get_outcome_display(),
            preview=row.preview,
            occurred_at=row.occurred_at,
            url=conversation_path(row.conversation),
        )
        for row in rows
    ]


def get_inbox(user: CustomUser, project_ids: list[int], *, limit: int) -> Inbox:
    """The user's open inbox, newest first, at most ``limit`` items per kind.

    Two list queries, plus one count per kind only when its list is full.
    """
    if not project_ids:
        return Inbox(failures=[], replies=[], failure_total=0, reply_total=0)
    failures = _failures(user, project_ids, limit)
    replies = _replies(user, project_ids, limit)
    failure_total = (
        _open_failures(user, project_ids).count()
        if len(failures) >= limit
        else len(failures)
    )
    reply_total = (
        _open_replies(user, project_ids).count()
        if len(replies) >= limit
        else len(replies)
    )
    return Inbox(
        failures=failures,
        replies=replies,
        failure_total=failure_total,
        reply_total=reply_total,
    )


def count_inbox(user: CustomUser, project_ids: list[int]) -> tuple[int, int]:
    """``(open missed failures, open missed replies)`` for the badge (two counts)."""
    if not project_ids:
        return 0, 0
    return (
        _open_failures(user, project_ids).count(),
        _open_replies(user, project_ids).count(),
    )
