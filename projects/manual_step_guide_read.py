"""Read a claimed Manual run case's "How to do it" guide (one query).

Used by the Test Runner snapshot, the web case card and Ask AI. A guide is
shown only while its fingerprint matches the case text and the verbatim steps
in the checkpoint, on the OS it was written for (``ManualStepGuide.os_name``,
the runner's OS at claim time; the live OS is never read here, so a runner
disconnect does not hide it); a guide in progress with no write for
``stale_after_seconds()`` counts as lost (its missing steps read "none").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Final, Literal

from django.conf import settings
from django.utils import timezone

from agents.checkpoint import checkpoint_from_json
from agents.os_shell import shell_label
from agents.types import PersonHowToItem, SubTask
from projects.case_task_text import build_task_description
from projects.manual_step_guide_fingerprint import guide_fingerprint
from projects.models import ManualGuideState, ManualStepGuide, TestRunTestCase
from projects.person_step_rows import stored_how_to

ManualStepHowToState = Literal["pending", "ready", "none"]

# States in which the guide task still writes steps.
IN_PROGRESS_GUIDE_STATES: Final = frozenset(
    {
        ManualGuideState.QUEUED,
        ManualGuideState.RESEARCHING,
        ManualGuideState.WRITING,
    }
)
# Per-step entry states the worker stores in ``ManualStepGuide.steps``.
STEP_READY: Final = "ready"
STEP_FAILED: Final = "failed"


@dataclass(frozen=True)
class ManualStepHowTo:
    """One verbatim step's guide: ``ready`` with items, ``pending`` while it
    is being written, ``none`` otherwise (failed, empty, beyond the cap,
    lost, or never requested)."""

    num: int
    state: ManualStepHowToState
    how_to: tuple[PersonHowToItem, ...] = ()


@dataclass(frozen=True)
class ManualGuideView:
    """A case's guide as the read surfaces show it."""

    # The shell the suggested commands are for ("Command Prompt (cmd.exe)").
    shell_label: str
    # Some step is still ``pending``.
    preparing: bool
    # One per verbatim step of the checkpoint, in order (``num`` 1..n).
    steps: tuple[ManualStepHowTo, ...]

    def step(self, num: int) -> ManualStepHowTo:
        """Step ``num``'s guide; ``none`` when out of range."""
        if 1 <= num <= len(self.steps):
            return self.steps[num - 1]
        return ManualStepHowTo(num=num, state="none")


def stale_after_seconds() -> int:
    """A guide in progress with no write for this long is treated as lost."""
    return (
        max(
            settings.PERSON_GUIDE_RESEARCH_TIMEOUT_SECONDS,
            settings.MANUAL_GUIDE_CHUNK_TIMEOUT_SECONDS,
        )
        + 60
    )


def manual_plan(pivot: TestRunTestCase) -> tuple[SubTask, ...]:
    """The verbatim steps the claim stored; () when not a manual checkpoint.

    No query unless ``execution_checkpoint`` is deferred."""
    checkpoint = checkpoint_from_json(pivot.execution_checkpoint)
    if checkpoint is None or not checkpoint.manual:
        return ()
    return checkpoint.plan


def plan_fingerprint(
    pivot: TestRunTestCase, plan: tuple[SubTask, ...], *, os_name: str
) -> str:
    """The guide fingerprint of ``pivot`` with ``plan`` on ``os_name``
    (``pivot.test_case`` must be loaded)."""
    return guide_fingerprint(
        build_task_description(pivot.test_case),
        [(task.description, task.expected_result) for task in plan],
        os_name,
    )


def load_manual_guide(pivot: TestRunTestCase) -> ManualGuideView | None:
    """The guide of a Manual run case; None when there is none to show.

    One query (the guide row). ``pivot`` must have ``test_case`` loaded and
    its ``execution_checkpoint``. None: no guide row, guides off, or a guide
    written for another case text or step list.
    """
    guide = (
        ManualStepGuide.objects.filter(test_run_test_case_id=pivot.pk)
        .only("fingerprint", "state", "os_name", "step_count", "steps", "updated_at")
        .first()
    )
    if guide is None or guide.state == ManualGuideState.OFF:
        return None
    plan = manual_plan(pivot)
    if (
        not plan
        or plan_fingerprint(pivot, plan, os_name=guide.os_name) != guide.fingerprint
    ):
        return None
    in_progress = (
        guide.state in IN_PROGRESS_GUIDE_STATES
        and guide.updated_at
        >= timezone.now() - timedelta(seconds=stale_after_seconds())
    )
    stored = guide.steps if isinstance(guide.steps, dict) else {}
    steps = tuple(
        _step_view(
            num, stored.get(str(num)), pending=in_progress and num <= guide.step_count
        )
        for num in range(1, len(plan) + 1)
    )
    return ManualGuideView(
        shell_label=shell_label(guide.os_name),
        preparing=any(step.state == "pending" for step in steps),
        steps=steps,
    )


def _step_view(num: int, entry: object, *, pending: bool) -> ManualStepHowTo:
    if isinstance(entry, dict):
        if entry.get("state") == STEP_READY:
            items = stored_how_to(entry.get("how_to"))
            return ManualStepHowTo(
                num=num, state="ready" if items else "none", how_to=items
            )
        return ManualStepHowTo(num=num, state="none")
    return ManualStepHowTo(num=num, state="pending" if pending else "none")
