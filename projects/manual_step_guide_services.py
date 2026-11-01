"""Use case: a Manual run case was claimed; (re)start its step guide.

Called inside ``claim_manual_case``'s transaction (run lock held), after the
claim wrote the verbatim checkpoint. No AI here: the guide row is created or
restarted and the guide task (``projects.tasks.prepare_manual_case_guide``,
``assist`` queue) is sent by name on commit, so a claim never waits on it.

Reuse: a re-claim with the same fingerprint keeps the research notes and the
steps already written and only writes the missing and failed ones; a new
fingerprint (case text, steps or runner OS changed) starts afresh. Every
restart bumps ``generation``, which fences the writes of an older task.
"""

from __future__ import annotations

import logging
from typing import Final

from django.conf import settings
from django.db import transaction

from agents.os_shell import get_os_name
from auto_tester.celery import app as celery_app
from projects.manual_step_guide_read import STEP_READY, manual_plan, plan_fingerprint
from projects.models import ManualGuideState, ManualStepGuide, Project, TestRunTestCase

logger = logging.getLogger(__name__)

PREPARE_MANUAL_CASE_GUIDE_TASK: Final = "projects.tasks.prepare_manual_case_guide"


def request_manual_case_guide(*, pivot: TestRunTestCase, project_id: int) -> int | None:
    """Start or resume the guide of a just-claimed case; the guide id when a
    guide task was queued (on commit), None otherwise.

    Must run inside the claim's transaction, with ``pivot.test_case`` loaded
    and its new ``execution_checkpoint`` set. Three queries.
    """
    # The runner's OS now (the only place the live OS is read): the guide
    # stores it, and every reader and chunk compares against the stored one.
    system_info = (
        Project.objects.filter(pk=project_id)
        .values_list("agent_system_info", flat=True)
        .first()
    )
    os_name = get_os_name(system_info if isinstance(system_info, dict) else None)
    plan = manual_plan(pivot)
    guide = (
        ManualStepGuide.objects.select_for_update()
        .filter(test_run_test_case_id=pivot.pk)
        .first()
    )
    max_steps = settings.MANUAL_GUIDE_MAX_STEPS
    if not plan or max_steps <= 0:
        if guide is not None and guide.state != ManualGuideState.OFF:
            # Fence an older task and hide what it wrote.
            guide.generation += 1
            guide.state = ManualGuideState.OFF
            guide.save(update_fields=["generation", "state", "updated_at"])
        return None
    fingerprint = plan_fingerprint(pivot, plan, os_name=os_name)
    step_count = min(len(plan), max_steps)
    if guide is None:
        guide = ManualStepGuide.objects.create(
            test_run_test_case=pivot,
            fingerprint=fingerprint,
            generation=1,
            state=ManualGuideState.QUEUED,
            os_name=os_name,
            step_count=step_count,
        )
    else:
        steps = guide.steps if isinstance(guide.steps, dict) else {}
        if guide.fingerprint == fingerprint:
            ready = {
                key: entry
                for key, entry in steps.items()
                if isinstance(entry, dict) and entry.get("state") == STEP_READY
            }
            if guide.state == ManualGuideState.DONE and all(
                str(num) in ready for num in range(1, step_count + 1)
            ):
                if guide.step_count != step_count:
                    guide.step_count = step_count
                    guide.save(update_fields=["step_count", "updated_at"])
                return None
            # Failed steps are written again; research notes are kept.
            guide.steps = ready
        else:
            guide.fingerprint = fingerprint
            guide.steps = {}
            guide.research = {}
        guide.generation += 1
        guide.state = ManualGuideState.QUEUED
        guide.os_name = os_name
        guide.step_count = step_count
        guide.save(
            update_fields=[
                "fingerprint",
                "generation",
                "state",
                "os_name",
                "step_count",
                "research",
                "steps",
                "updated_at",
            ]
        )
    guide_id, generation = guide.pk, guide.generation
    transaction.on_commit(lambda: send_manual_guide_task(guide_id, generation))
    return guide_id


def send_manual_guide_task(guide_id: int, generation: int) -> None:
    """Queue one guide chunk; never raises (a lost chunk reads as stale)."""
    try:
        celery_app.send_task(
            PREPARE_MANUAL_CASE_GUIDE_TASK, args=[guide_id, generation]
        )
    except Exception:
        logger.warning(
            "Could not queue the step guide %s (generation %s)",
            guide_id,
            generation,
            exc_info=True,
        )
