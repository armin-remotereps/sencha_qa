"""Query pieces shared by the case analysis services (no I/O at import)."""

from __future__ import annotations

from django.db.models import BooleanField, Case, Q, Value, When

from projects.models import TestRunTestCaseStatus
from run_triage.case_scope import ANALYSED_STATUSES

# ``case_scope`` as a filter: a Cancelled case counts only with a result.
_BLANK = r"^\s*$"


def analysable_q(prefix: str = "") -> Q:
    """Run cases the analysis classifies (``CaseScope.ANALYSED``)."""
    return Q(**{f"{prefix}status__in": ANALYSED_STATUSES}) | (
        Q(**{f"{prefix}status": TestRunTestCaseStatus.CANCELLED})
        & ~Q(**{f"{prefix}result__regex": _BLANK})
    )


def has_result_annotation() -> Case:
    """True when the case's ``result`` holds more than whitespace."""
    return Case(
        When(result__regex=_BLANK, then=Value(False)),
        default=Value(True),
        output_field=BooleanField(),
    )
