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

from collections.abc import Iterable, Mapping
from typing import Final

from controller_client.protocol import (
    ClientCapability,
    ManualCaseVerdictWire,
    ManualShotKind,
)
from projects.models import ManualVerdict, RunHelpMode, TestRun

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
MANUAL_PAUSE_REFUSED: Final = (
    "Manual cases cannot be paused: release the case at the Test Runner instead."
)
MANUAL_REDESIGN_REFUSED: Final = (
    "A manual case follows the test case's own steps; its plan cannot be redesigned."
)
MANUAL_MODE_SWITCH_REFUSED: Final = (
    "A run can be switched to or from Manual only while it is a draft."
)
# Readiness: a manual run needs a runner whose window supports it.
MANUAL_RUNNER_OUTDATED: Final = (
    "Update the Test Runner (and enable its window) to run manual tests."
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


def runner_supports_manual(capabilities: Iterable[str]) -> bool:
    """The connected runner advertises MANUAL_RUN_V1 (only with its window)."""
    return ClientCapability.MANUAL_RUN_V1 in set(capabilities)


def manual_attempt_id(shot_key: str) -> str:
    """The attempt id of a manual step: "m" + the step's screenshot key.

    The same id goes on the step's ``TestRunScreenshot.attempt_id``, so the
    Steps card and the TestRail evidence link shots to steps as for agent
    attempts. Fits the 32-character column (keys are ≤ 24 characters).
    """
    return f"{_MANUAL_ATTEMPT_PREFIX}{shot_key}"
