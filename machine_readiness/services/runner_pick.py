"""Use case: which Test Runner the Machine readiness tab shows and scans.

The picker lists the viewer's own runners usable in the project
(``runners.services.runner_access.load_usable_runners``, labels from
``runners.choices``). The shown runner is the one asked for (validated by
``forms.RunnerPickForm``) when it is among them, else the first connected
one, else the first; None when the viewer has none (read-only members, or no
runner yet): the inventory then shows only a scan from before user runners.
"""

from __future__ import annotations

from dataclasses import dataclass

from accounts.models import CustomUser
from projects.models import Project
from runners.choices import RunnerChoice, runner_choice_list
from runners.models import TestRunner
from runners.services.runner_access import load_usable_runners


@dataclass(frozen=True)
class RunnerPick:
    # (runner id, label) of the viewer's usable runners, by name.
    choices: tuple[RunnerChoice, ...]
    selected: TestRunner | None


def pick_runner(user: CustomUser, project: Project, wanted: int | None) -> RunnerPick:
    """The picker's choices and the shown runner (two queries at most).

    ``wanted``: the runner id the page asked for (``RunnerPickForm``), or None.
    """
    runners = load_usable_runners(user, project)
    selected = next((r for r in runners if r.pk == wanted), None)
    if selected is None:
        selected = next((r for r in runners if r.connected), None)
    if selected is None and runners:
        selected = runners[0]
    return RunnerPick(choices=tuple(runner_choice_list(runners)), selected=selected)
