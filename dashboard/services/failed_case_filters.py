"""The Failed cases scope and its filters: the single source for the Failures
page list, its by-case grouping, the CSV export, the dashboard widget and the
bulk actions.

Every query starts from ``failed_case_queryset``: Failed run cases of the given
(member, non-archived) projects. ``failed_by`` and the review state are only
meaningful while the case is Failed, which this scope guarantees.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import urlencode

from django.db import models
from django.db.models import BooleanField, ExpressionWrapper, Q, QuerySet
from django.db.models.functions import Substr

from projects.models import FailedBy, TestRunTestCase, TestRunTestCaseStatus

# Characters of the verdict loaded for the list (the full text can be large).
VERDICT_PREVIEW_LENGTH = 200

# ``Project.has_testrail_settings`` of the row's project, as an expression
# (the three columns are NOT NULL, default "").
_TESTRAIL_CONFIGURED = ExpressionWrapper(
    ~Q(test_run__project__testrail_url="")
    & ~Q(test_run__project__testrail_email="")
    & ~Q(test_run__project__testrail_api_key_encrypted=""),
    output_field=BooleanField(),
)

# "C123" or "123": a TestRail case id typed into the search box.
_TESTRAIL_ID_RE = re.compile(r"^[Cc]?(\d{1,18})$")


class ReviewFilter(models.TextChoices):
    """Review state of a Failed case (the blank value means any)."""

    NOT_CHECKED = "not_checked", "Not checked"
    CHECKED = "checked", "Checked, not sent"
    SENT = "sent", "Sent to TestRail"


class FailedByFilter(models.TextChoices):
    """Who made the case Failed (``FailedBy`` plus "not recorded")."""

    AGENT = FailedBy.AGENT.value, FailedBy.AGENT.label
    HELP = FailedBy.HELP.value, FailedBy.HELP.label
    REVIEW = FailedBy.REVIEW.value, FailedBy.REVIEW.label
    NOT_RECORDED = "none", "Not recorded"


class FailedCaseView(models.TextChoices):
    """The two layouts of the Failures page."""

    CASES = "cases", "Cases"
    BY_CASE = "by_case", "By test case"


@dataclass(frozen=True)
class FailedCaseFilters:
    """Cleaned Failures page filters; every field optional (all time, any)."""

    date_from: date | None = None
    date_to: date | None = None
    project_id: int | None = None
    run_id: int | None = None
    query: str = ""
    review: str = ""
    failed_by: str = ""


def _start_of_day(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


def _search_q(query: str) -> Q:
    """Title contains ``query``, or the TestRail id matches ``C123`` / ``123``."""
    condition = Q(test_case__title__icontains=query)
    match = _TESTRAIL_ID_RE.match(query)
    if match:
        number = match.group(1)
        condition |= Q(test_case__testrail_id=number) | Q(
            test_case__testrail_id__iexact=f"C{number}"
        )
    return condition


def _review_q(review: str) -> Q | None:
    if review == ReviewFilter.NOT_CHECKED:
        return Q(checked_at__isnull=True)
    if review == ReviewFilter.CHECKED:
        return Q(checked_at__isnull=False, testrail_source_result_id__isnull=True)
    if review == ReviewFilter.SENT:
        return Q(testrail_source_result_id__isnull=False)
    return None


def failed_case_queryset(
    project_ids: Sequence[int], filters: FailedCaseFilters
) -> QuerySet[TestRunTestCase]:
    """Failed run cases of ``project_ids`` matching ``filters`` (no ordering,
    no column selection; callers shape it).

    ``project_ids`` must already be the user's member, non-archived projects;
    ``filters.project_id`` and ``filters.run_id`` only narrow that scope, so a
    forged id can never widen it. Dates are UTC days on ``finished_at``,
    ``date_to`` inclusive, expressed as a half-open range so the
    ``(status, -finished_at)`` index is usable.
    """
    queryset = TestRunTestCase.objects.filter(
        status=TestRunTestCaseStatus.FAILED,
        test_run__project_id__in=list(project_ids),
    )
    if filters.project_id is not None:
        queryset = queryset.filter(test_run__project_id=filters.project_id)
    if filters.run_id is not None:
        queryset = queryset.filter(test_run_id=filters.run_id)
    if filters.date_from is not None:
        queryset = queryset.filter(finished_at__gte=_start_of_day(filters.date_from))
    if filters.date_to is not None:
        queryset = queryset.filter(
            finished_at__lt=_start_of_day(filters.date_to + timedelta(days=1))
        )
    if filters.query:
        queryset = queryset.filter(_search_q(filters.query))
    review_q = _review_q(filters.review)
    if review_q is not None:
        queryset = queryset.filter(review_q)
    if filters.failed_by == FailedByFilter.NOT_RECORDED:
        queryset = queryset.filter(failed_by="")
    elif filters.failed_by:
        queryset = queryset.filter(failed_by=filters.failed_by)
    return queryset


def failed_case_list_queryset(
    project_ids: Sequence[int], filters: FailedCaseFilters
) -> QuerySet[TestRunTestCase]:
    """``failed_case_queryset`` shaped for a list: newest failure first, the
    case, run and project joined, only the shown columns, and the verdict
    truncated in the database (``verdict``). ``result``, ``logs`` and
    ``execution_checkpoint`` are never loaded.

    ``testrail_configured`` mirrors ``Project.has_testrail_settings`` in SQL
    (URL, email and encrypted key all non-empty), so the encrypted key is
    never loaded into memory.
    """
    return (
        failed_case_queryset(project_ids, filters)
        .select_related("test_case", "test_run", "test_run__project")
        .only(
            "id",
            "status",
            "failed_by",
            "finished_at",
            "checked_at",
            "testrail_source_result_id",
            "testrail_source_run_id",
            "test_case_id",
            "test_case__id",
            "test_case__title",
            "test_case__testrail_id",
            "test_run_id",
            "test_run__id",
            "test_run__name",
            "test_run__status",
            "test_run__project_id",
            "test_run__project__id",
            "test_run__project__name",
        )
        .annotate(
            verdict=Substr("result", 1, VERDICT_PREVIEW_LENGTH),
            testrail_configured=_TESTRAIL_CONFIGURED,
        )
        .order_by("-finished_at", "-id")
    )


def encode_filters(filters: FailedCaseFilters, *, view: str = "") -> str:
    """The query string of the non-empty filters (no leading ``?``; never
    page or per_page). ``view`` is kept unless it is the default list."""
    params: list[tuple[str, str]] = []
    if filters.date_from is not None:
        params.append(("date_from", filters.date_from.isoformat()))
    if filters.date_to is not None:
        params.append(("date_to", filters.date_to.isoformat()))
    if filters.project_id is not None:
        params.append(("project", str(filters.project_id)))
    if filters.run_id is not None:
        params.append(("run", str(filters.run_id)))
    if filters.query:
        params.append(("q", filters.query))
    if filters.review:
        params.append(("review", filters.review))
    if filters.failed_by:
        params.append(("failed_by", filters.failed_by))
    if view and view != FailedCaseView.CASES:
        params.append(("view", view))
    return urlencode(params)


def review_state_label(
    checked_at: datetime | None, testrail_source_result_id: int | None
) -> str:
    """ "Sent to TestRail", "Checked, not sent" or "Not checked"."""
    if testrail_source_result_id is not None:
        return str(ReviewFilter.SENT.label)
    if checked_at is not None:
        return str(ReviewFilter.CHECKED.label)
    return str(ReviewFilter.NOT_CHECKED.label)


def failed_by_label(failed_by: str) -> str:
    """The ``FailedByFilter`` label of a stored ``failed_by`` value."""
    if not failed_by:
        return str(FailedByFilter.NOT_RECORDED.label)
    try:
        return str(FailedByFilter(failed_by).label)
    except ValueError:
        return failed_by
