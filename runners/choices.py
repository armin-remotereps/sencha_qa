"""Runner pickers' choices: the one label builder (pure, no queries).

Every select that offers a person their own Test Runners (run forms, the
Machine setup chat header, the Machine readiness tab) builds its choices
here from ``runner_access.load_usable_runners`` rows.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from runners.models import TestRunner

# (runner id, label) as a form's choices; the label is plain text.
RunnerChoice = tuple[int, str]

_NOT_CONNECTED_SUFFIX: Final = " (not connected)"


def runner_choice(runner: TestRunner) -> RunnerChoice:
    """``runner``'s choice: its name, marked when it is not connected."""
    label = runner.name if runner.connected else runner.name + _NOT_CONNECTED_SUFFIX
    return (runner.pk, label)


def runner_choice_list(runners: Iterable[TestRunner]) -> list[RunnerChoice]:
    return [runner_choice(runner) for runner in runners]
