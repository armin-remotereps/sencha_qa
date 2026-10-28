"""A user's "Needs your attention" list and its badge count.

Four groups: cases that need help (open ``CaseHelpRequest`` rows, listed while
open, including while the Night auto-fix is trying), person steps waiting for a
member ("Your turn", Human in the loop), missed failures and missed replies
(the last three from ``notifications.services.inbox``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.db.models import QuerySet
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from dashboard.services.member_projects import member_project_ids
from notifications.services.inbox import (
    Inbox,
    InboxPersonStep,
    count_inbox,
    get_inbox,
    open_person_steps,
    person_steps,
)
from projects.models import CaseHelpRequest, HelpAutoFixState, HelpResolution

# Items shown per group; the group says "and N more" past it.
ATTENTION_GROUP_LIMIT = 10


@dataclass(frozen=True)
class HelpItem:
    """One open help request: a paused case waiting for a person (or auto-fix)."""

    help_request_id: int
    pivot_id: int
    case_title: str
    testrail_id: str
    project_name: str
    run_name: str
    # Label of the infrastructure fault; empty when the agent asked.
    fault_label: str
    # Model-written (render escaped).
    ask: str
    auto_fix_trying: bool
    requested_at: datetime
    url: str


@dataclass(frozen=True)
class Attention:
    """Everything the "Needs your attention" card renders."""

    help_items: list[HelpItem]
    help_total: int
    person_items: list[InboxPersonStep]
    person_total: int
    inbox: Inbox
    # When the card was built: "Dismiss all" only clears items up to it.
    rendered_at: datetime

    @property
    def total(self) -> int:
        return (
            self.help_total
            + self.person_total
            + self.inbox.failure_total
            + self.inbox.reply_total
        )

    @property
    def help_more(self) -> int:
        return self.help_total - len(self.help_items)

    @property
    def person_more(self) -> int:
        return self.person_total - len(self.person_items)

    @property
    def failure_more(self) -> int:
        return self.inbox.failure_total - len(self.inbox.failures)

    @property
    def reply_more(self) -> int:
        return self.inbox.reply_total - len(self.inbox.replies)


def _open_help_requests(project_ids: list[int]) -> QuerySet[CaseHelpRequest]:
    return CaseHelpRequest.objects.filter(
        resolution=HelpResolution.OPEN,
        test_run_test_case__test_run__project_id__in=project_ids,
    )


def _help_item(help_request: CaseHelpRequest) -> HelpItem:
    pivot = help_request.test_run_test_case
    test_run = pivot.test_run
    return HelpItem(
        help_request_id=help_request.pk,
        pivot_id=pivot.pk,
        case_title=pivot.test_case.title,
        testrail_id=pivot.test_case.testrail_id,
        project_name=test_run.project.name,
        run_name=test_run.display_name,
        fault_label=help_request.get_fault_display() if help_request.fault else "",
        ask=help_request.ask,
        auto_fix_trying=help_request.auto_fix_state == HelpAutoFixState.RUNNING,
        requested_at=help_request.requested_at,
        url=reverse(
            "projects:test_run_case_detail",
            kwargs={
                "project_id": test_run.project_id,
                "test_run_id": test_run.pk,
                "pivot_id": pivot.pk,
            },
        ),
    )


def _help_items(project_ids: list[int], limit: int) -> list[HelpItem]:
    rows = (
        _open_help_requests(project_ids)
        .select_related(
            "test_run_test_case__test_case", "test_run_test_case__test_run__project"
        )
        .only(
            "id",
            "fault",
            "ask",
            "auto_fix_state",
            "requested_at",
            "test_run_test_case__id",
            "test_run_test_case__test_case__id",
            "test_run_test_case__test_case__title",
            "test_run_test_case__test_case__testrail_id",
            "test_run_test_case__test_run__id",
            "test_run_test_case__test_run__name",
            "test_run_test_case__test_run__project__id",
            "test_run_test_case__test_run__project__name",
        )
        .order_by("-requested_at", "-id")[:limit]
    )
    return [_help_item(row) for row in rows]


def get_attention(
    user: CustomUser, project_ids: list[int], *, now: datetime | None = None
) -> Attention:
    """The attention card for ``user`` over ``project_ids`` (their member projects).

    One help list query and one person step list query (each plus one count
    only when its list is full) and the inbox's queries; none when
    ``project_ids`` is empty.
    """
    rendered_at = now or timezone.now()
    if not project_ids:
        help_items: list[HelpItem] = []
        help_total = 0
        person_items: list[InboxPersonStep] = []
        person_total = 0
    else:
        help_items = _help_items(project_ids, ATTENTION_GROUP_LIMIT)
        help_total = (
            _open_help_requests(project_ids).count()
            if len(help_items) >= ATTENTION_GROUP_LIMIT
            else len(help_items)
        )
        person_items = person_steps(project_ids, ATTENTION_GROUP_LIMIT)
        person_total = (
            open_person_steps(project_ids).count()
            if len(person_items) >= ATTENTION_GROUP_LIMIT
            else len(person_items)
        )
    return Attention(
        help_items=help_items,
        help_total=help_total,
        person_items=person_items,
        person_total=person_total,
        inbox=get_inbox(user, project_ids, limit=ATTENTION_GROUP_LIMIT),
        rendered_at=rendered_at,
    )


def count_attention(user: CustomUser) -> int:
    """How many items need ``user``: the nav badge and tab title (5 queries)."""
    project_ids = member_project_ids(user)
    if not project_ids:
        return 0
    help_total = _open_help_requests(project_ids).count()
    person_total = open_person_steps(project_ids).count()
    failures, replies = count_inbox(user, project_ids)
    return help_total + person_total + failures + replies
