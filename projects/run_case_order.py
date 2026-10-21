"""The one ordering rule for a run's cases: TestRail order.

Cases are grouped by TestRail suite (so a run mixing suites runs one suite
after the other), then sorted by their rank in that suite. Cases without a
TestRail order (manual cases) come last. No queries live here.
"""

from __future__ import annotations

from django.db.models import F
from django.db.models.expressions import OrderBy

# Ordering for `TestRunTestCase` querysets.
RUN_CASE_ORDERING: tuple[OrderBy | str, ...] = (
    F("test_case__testrail_suite_id").asc(nulls_last=True),
    F("test_case__testrail_position").asc(nulls_last=True),
    "test_case_id",
)

# Ordering for `TestCase` querysets (the run case picker).
PICKER_CASE_ORDERING: tuple[OrderBy | str, ...] = (
    F("testrail_suite_id").asc(nulls_last=True),
    F("testrail_position").asc(nulls_last=True),
    "title",
)
