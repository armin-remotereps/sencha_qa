"""Whether a run is using the test machine now (a leaf: imports only models).

A Manual run stays STARTED for its whole life but no agent drives the
machine; only its tester does, while a claimed case is not paused. Machine
setup sends and runner actions read these two checks instead of "a run is
STARTED", so ``machine_setup`` and ``machine_readiness`` (through its ports)
share one definition.
"""

from __future__ import annotations

from projects.models import (
    RunHelpMode,
    TestRun,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)


def agent_run_started(project_id: int) -> bool:
    """A STARTED run of the project that is not Manual (one query)."""
    return (
        TestRun.objects.filter(project_id=project_id, status=TestRunStatus.STARTED)
        .exclude(help_mode=RunHelpMode.MANUAL)
        .exists()
    )


def manual_tester_on_machine(project_id: int) -> bool:
    """A claimed, unpaused IN_PROGRESS case of a STARTED Manual run (one query).

    Same rule as ``manual_run_policy.is_claimed_manual_case`` plus not paused,
    written as a filter; keep both aligned.
    """
    return (
        TestRunTestCase.objects.filter(
            test_run__project_id=project_id,
            test_run__status=TestRunStatus.STARTED,
            test_run__help_mode=RunHelpMode.MANUAL,
            status=TestRunTestCaseStatus.IN_PROGRESS,
            manual_paused_at__isnull=True,
        )
        .exclude(manual_claim_token="")
        .exists()
    )
