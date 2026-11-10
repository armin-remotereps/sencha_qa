"""The Test Runners a person may pick for a run of a project (form choices),
and their own runners as the onboarding wizard lists them.

Only their own runners, while they are a writable member of the project
(``runners.services.runner_access.load_usable_runners``); labels from
``runners.choices``. The choices double as the forms' validation: a runner
id outside them is refused before any service runs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from accounts.models import CustomUser
from projects.models import Project
from runners.choices import RunnerChoice, runner_choice_list
from runners.machine_state import runner_machine_state
from runners.models import TestRunner
from runners.services.runner_access import load_usable_runners
from visual_engine.types import VisualEngineStatus

__all__ = ["OwnRunner", "RunnerChoice", "list_own_runners", "runner_choices"]


@dataclass(frozen=True)
class OwnRunner:
    runner_id: int
    # Owner-given name (escaped on render).
    name: str
    # A ``runners.machine_state.MachineState`` value (badge_machine.html).
    machine_state: str


def runner_choices(
    user: CustomUser,
    project: Project,
    *,
    runners: Sequence[TestRunner] | None = None,
) -> list[RunnerChoice]:
    """The user's usable runners, by name (two queries at most; none when
    the caller passes the ``load_usable_runners`` rows)."""
    if runners is None:
        runners = load_usable_runners(user, project)
    return runner_choice_list(runners)


def list_own_runners(
    user: CustomUser,
    project: Project,
    visual_engine: VisualEngineStatus,
    *,
    runners: Sequence[TestRunner] | None = None,
) -> list[OwnRunner]:
    """The user's usable runners with their badge state (two queries at most)."""
    if runners is None:
        runners = load_usable_runners(user, project)
    return [
        OwnRunner(
            runner_id=runner.pk,
            name=runner.name,
            machine_state=runner_machine_state(runner, visual_engine).value,
        )
        for runner in runners
    ]
