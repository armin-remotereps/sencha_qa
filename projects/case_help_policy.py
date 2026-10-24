"""Whether a Need help case may be handed to the Night-mode auto-fix.

Q10: each step of a case gets at most one auto-fix that reported the machine
fixed per from-scratch execution of the case. A from-scratch execution starts
at the run's Start (``TestRun.started_at``) or at a re-run in place
(``TestRunCaseResume`` of kind ``rerun``); a Resume or a Continue after help
does not start one. When the same step fails again after such a fix, it goes
straight to a person.
"""

from __future__ import annotations

from django.db.models import F, Q, Subquery

from projects.models import (
    CaseHelpRequest,
    CaseRestartKind,
    HelpAutoFixState,
    TestRunCaseResume,
)


def auto_fix_eligible(pivot_id: int, step_num: int) -> bool:
    """False when this step already got a fixed auto-fix since the case last
    started from scratch (Start, or a re-run in place); a repeat goes to a person.

    One query: an EXISTS over the pivot's earlier fixed requests for the step,
    bounded below by the run's start (a join) and the latest re-run (a
    correlated subquery). A NULL bound imposes no limit.
    """
    last_rerun_at = Subquery(
        TestRunCaseResume.objects.filter(
            test_run_test_case_id=pivot_id, kind=CaseRestartKind.RERUN
        )
        .order_by("-created_at")
        .values("created_at")[:1]
    )
    fixed_since_scratch = (
        CaseHelpRequest.objects.filter(
            test_run_test_case_id=pivot_id,
            step_num=step_num,
            auto_fix_state=HelpAutoFixState.FIXED,
        )
        .annotate(last_rerun_at=last_rerun_at)
        .filter(
            Q(test_run_test_case__test_run__started_at__isnull=True)
            | Q(requested_at__gte=F("test_run_test_case__test_run__started_at"))
        )
        .filter(Q(last_rerun_at__isnull=True) | Q(requested_at__gte=F("last_rerun_at")))
    )
    return not fixed_since_scratch.exists()
