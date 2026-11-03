"""The run a case analysis page is about, scoped to its project."""

from __future__ import annotations

from projects.models import Project, TestRun
from run_triage.exceptions import RunTriageError


def get_run_for_project(project: Project, run_id: int) -> TestRun:
    test_run = TestRun.objects.filter(id=run_id, project=project).first()
    if test_run is None:
        raise RunTriageError("This run does not exist.")
    return test_run
