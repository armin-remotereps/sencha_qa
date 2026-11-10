"""Manual run rules shared by every path that must tell manual runs apart.

A leaf module: it imports only ``projects.models`` and the controller
protocol, so services, the consumer, TestRail and evidence code can all use it
without an import cycle. No queries.

A run's ``help_mode`` says whether the run is manual *now*; project scope
"all" can rewrite finished runs, so code that judges an existing result (push,
evidence, the case page) asks the case's checkpoint (``is_manual_checkpoint``)
instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from controller_client.protocol import (
    ManualCaseVerdictWire,
    ManualShotKind,
)
from projects.models import (
    ManualVerdict,
    RunHelpMode,
    TestRun,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)

# User-safe refusals of the agent-only paths for a manual run or case.
MANUAL_RESUME_REFUSED: Final = "A manual case can be resumed only in a Manual run."
# Manual Resume (``projects.manual_run_resume_services``).
MANUAL_NOTHING_RESUMABLE: Final = (
    "There are no Cancelled cases to resume. To test a finished case again, "
    "re-run it."
)
MANUAL_RESUME_NOT_MANUAL: Final = "This run is not a Manual run."
MANUAL_HELP_REFUSED: Final = "Manual runs have no help requests."
MANUAL_PERSON_STEP_REFUSED: Final = "Manual runs have no person steps."
# The agent pause path refuses manual cases; they pause from the Manual test
# card or the Test Runner window (``projects.manual_case_pause_services``).
MANUAL_PAUSE_REFUSED: Final = (
    "Pause a manual case from its Manual test card or the Test Runner window."
)
# Manual pause and discuss (``manual_case_pause_services``,
# ``machine_setup.services.open_manual_chat``).
MANUAL_RUN_NOT_RUNNING: Final = "This manual run is not running."
MANUAL_PAUSE_NOT_CLAIMED: Final = (
    "Only a case someone is testing can be paused or continued."
)
MANUAL_PAUSE_NOT_PAUSED: Final = "The case is not paused."
MANUAL_PAUSE_AGENT_BUSY: Final = (
    "The agent is working on the machine. Wait for it to finish or stop it first."
)
MANUAL_PAUSE_STALE_CLAIM: Final = (
    "This case was released or handed in. Refresh the Test Runner."
)
MANUAL_DISCUSS_NOT_CLAIMED: Final = (
    "This case was handed in or released. Use Discuss result after hand-in."
)
MANUAL_DISCUSS_MACHINE_NEEDS_PAUSE: Final = (
    "Pause the case first: the tester is using the machine."
)
# Any setup thread but the case's own discussion (``runner_use``).
MANUAL_TESTER_ON_MACHINE: Final = (
    "A tester is performing a manual case on this machine. Try again once "
    "the case is paused, handed in or released."
)
MANUAL_REDESIGN_REFUSED: Final = (
    "A manual case follows the test case's own steps; its plan cannot be redesigned."
)
MANUAL_MODE_SWITCH_REFUSED: Final = (
    "A run can be switched to or from Manual only while it is a draft."
)

# ``TestRunScreenshot.tool_name`` of each kind of manual screenshot.
MANUAL_SHOT_TOOL_NAMES: Final[Mapping[ManualShotKind, str]] = {
    ManualShotKind.PASS: "manual_pass",
    ManualShotKind.FAIL: "manual_fail",
    ManualShotKind.CASE: "manual_case",
}
MANUAL_TOOL_NAMES: Final[frozenset[str]] = frozenset(MANUAL_SHOT_TOOL_NAMES.values())

# The wire verdict to the stored one (whose value is also the case status).
MANUAL_WIRE_VERDICTS: Final[Mapping[ManualCaseVerdictWire, ManualVerdict]] = {
    ManualCaseVerdictWire.PASSED: ManualVerdict.SUCCESS,
    ManualCaseVerdictWire.FAILED: ManualVerdict.FAILED,
    ManualCaseVerdictWire.BLOCKED: ManualVerdict.BLOCKED,
}

# Prefix of a manual step attempt's ``SubTaskAttempt.attempt_id``.
_MANUAL_ATTEMPT_PREFIX: Final = "m"
_MANUAL_CHECKPOINT_KEY: Final = "manual"


def is_manual_mode(help_mode: str) -> bool:
    return help_mode == RunHelpMode.MANUAL


def is_manual_run(run: TestRun) -> bool:
    """The run is in Manual mode now (no agent, no chain)."""
    return is_manual_mode(run.help_mode)


def is_claimed_manual_case(test_run: TestRun, pivot: TestRunTestCase) -> bool:
    """``pivot`` is the claimed IN_PROGRESS case of ``test_run``, a STARTED
    manual run (loaded fields only, no query)."""
    return (
        test_run.status == TestRunStatus.STARTED
        and is_manual_run(test_run)
        and pivot.status == TestRunTestCaseStatus.IN_PROGRESS
        and bool(pivot.manual_claim_token)
    )


def crosses_manual_boundary(old_mode: str, new_mode: str) -> bool:
    """A mode change into or out of Manual (allowed on drafts only, D11)."""
    return is_manual_mode(old_mode) != is_manual_mode(new_mode)


def is_manual_checkpoint(checkpoint_json: object) -> bool:
    """The stored ``execution_checkpoint`` belongs to a manual case.

    Reads only the optional ``manual`` key (a strict JSON true, as
    ``agents.checkpoint`` parses it), so it costs no full parse.
    """
    return (
        isinstance(checkpoint_json, dict)
        and checkpoint_json.get(_MANUAL_CHECKPOINT_KEY) is True
    )


def manual_attempt_id(shot_key: str) -> str:
    """The attempt id of a manual step: "m" + the step's screenshot key.

    The same id goes on the step's ``TestRunScreenshot.attempt_id``, so the
    Steps card and the TestRail evidence link shots to steps as for agent
    attempts. Fits the 32-character column (keys are ≤ 24 characters).
    """
    return f"{_MANUAL_ATTEMPT_PREFIX}{shot_key}"
