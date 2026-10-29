from __future__ import annotations

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from accounts.types import AuthenticatedRequest
from projects.decorators import project_membership_required
from projects.exceptions import HelpModeNotAllowedError, RunHelpError
from projects.forms import ProjectRunModeForm
from projects.models import Project, RunHelpMode
from projects.project_run_mode_services import (
    ProjectRunModeChange,
    change_project_run_mode,
)
from projects.testrail_views import render_project_settings


@project_membership_required
@require_POST
def settings_run_mode(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    """Settings "Run mode" card: set the project mode and apply it to runs."""
    form = ProjectRunModeForm(request.POST)
    if not form.is_valid():
        return render_project_settings(request, project, run_mode_form=form)
    help_mode: str = form.cleaned_data["help_mode"]
    try:
        change = change_project_run_mode(
            project=project,
            user=request.user,
            help_mode=help_mode,
            scope=form.cleaned_data["apply_to"],
        )
    except (HelpModeNotAllowedError, RunHelpError) as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, _format_change(help_mode, change))
    return redirect("projects:settings", project_id=project.id)


def _count(number: int, singular: str, plural: str) -> str:
    return f"{number} {singular if number == 1 else plural}"


def _format_change(help_mode: str, change: ProjectRunModeChange) -> str:
    """One flash line with the counts, e.g. "Updated 3 drafts and 1 active run"."""
    label = RunHelpMode(help_mode).label
    head = (
        f"Run mode set to {label}."
        if change.changed
        else f"The run mode was already {label}."
    )
    parts: list[str] = []
    if change.drafts_updated:
        parts.append(_count(change.drafts_updated, "draft", "drafts"))
    if change.finished_updated:
        parts.append(_count(change.finished_updated, "finished run", "finished runs"))
    if change.active_switched:
        parts.append(_count(change.active_switched, "active run", "active runs"))
    sentences = [head]
    if parts:
        joined = (
            parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + f" and {parts[-1]}"
        )
        sentences.append(f"Updated {joined}.")
    if change.handed_back:
        sentences.append(
            f"{_count(change.handed_back, 'step', 'steps')} waiting for a person "
            "went back to the agent."
        )
    if change.active_skipped:
        sentences.append(
            f"{_count(change.active_skipped, 'run', 'runs')} could not be "
            "switched (finished meanwhile)."
        )
    return " ".join(sentences)
