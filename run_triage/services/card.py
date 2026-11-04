"""The run page's "Case analysis" card and the Analyze blocker."""

from __future__ import annotations

from django.conf import settings
from django.utils import timezone

from machine_setup.turn_state import MACHINE_SETUP_RUNNING_MESSAGE, live_turn_exists
from projects.models import TestRun, TestRunStatus
from projects.run_machine_use import agent_run_started
from projects.runner_update_state import (
    RUNNER_UPDATE_RUNNING_MESSAGE,
    runner_update_in_progress,
)
from run_triage.services.triage_state import category_counts, load_state, thread_state
from run_triage.types import TriageCard


def analyze_blocker_for(test_run: TestRun, *, analysable: int, turn_live: bool) -> str:
    """Why Analyze would be refused now ("" = allowed); at most three queries.

    The send gates of the analysis thread (``machine_setup.services.send_gates``:
    live turn, agent-driven Started run, Test Runner update; no READY gate),
    plus what the opener refuses (draft, nothing analysable, too many cases).
    """
    if test_run.status == TestRunStatus.WAITING:
        return "This run is a draft: start it before analysing its cases."
    if analysable == 0:
        return "No case of this run has a result to analyse yet."
    if analysable > settings.RUN_TRIAGE_MAX_CASES:
        return (
            f"This run has {analysable} cases to analyse; at most "
            f"{settings.RUN_TRIAGE_MAX_CASES} can be analysed."
        )
    if turn_live:
        return "The analysis is running in Machine setup."
    now = timezone.now()
    if live_turn_exists(test_run.project_id, now):
        return MACHINE_SETUP_RUNNING_MESSAGE
    if agent_run_started(test_run.project_id):
        return (
            "An agent run of this project is running. Analyse when it "
            "finishes or pauses."
        )
    if runner_update_in_progress(test_run.project_id, now):
        return RUNNER_UPDATE_RUNNING_MESSAGE
    return ""


def get_triage_card(test_run: TestRun) -> TriageCard:
    """The run page's card: the pivot/row join, the thread, and up to two
    blocker checks (about four queries)."""
    state = load_state(test_run.id)
    thread_id, turn_live = thread_state(test_run.id)
    return TriageCard(
        test_run_id=test_run.id,
        analysable_count=state.analysable,
        classified_count=len(state.rows),
        stale_count=state.stale,
        unclassified_count=state.unclassified,
        categories=category_counts(state.rows),
        thread_id=thread_id,
        turn_live=turn_live,
        analyze_blocker=analyze_blocker_for(
            test_run, analysable=state.analysable, turn_live=turn_live
        ),
    )
