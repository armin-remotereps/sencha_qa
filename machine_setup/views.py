from __future__ import annotations

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from accounts.types import AuthenticatedRequest
from machine_setup.exceptions import (
    MachineSetupError,
    SetupMachineNotReadyError,
    SetupNoActiveTurnError,
    SetupTurnActiveError,
)
from machine_setup.forms import SetupMessageForm
from machine_setup.services.chat_page import get_setup_page
from machine_setup.services.reset_conversation import reset_conversation
from machine_setup.services.send_message import send_setup_message
from machine_setup.services.stop_turn import request_stop
from projects.decorators import project_membership_required
from projects.forms import form_error_text
from projects.models import Project


@project_membership_required
@require_GET
def setup_chat(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    page = get_setup_page(project, include_messages=False)
    return render(
        request,
        "machine_setup/chat.html",
        {
            "project": project,
            "page": page,
            "visual_engine": page.visual_engine,
            "blocker_texts": {
                "machine_not_ready": SetupMachineNotReadyError.default_message,
                "turn_active": SetupTurnActiveError.default_message,
            },
            "form": SetupMessageForm(),
            "active_tab": "machine_setup",
            "active_nav": "projects",
        },
    )


@project_membership_required
@require_POST
def setup_send(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    form = SetupMessageForm(request.POST)
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return redirect("machine_setup:chat", project_id=project.id)
    try:
        send_setup_message(
            project=project, user=request.user, text=form.cleaned_data["text"]
        )
    except MachineSetupError as exc:
        messages.error(request, exc.message)
    return redirect("machine_setup:chat", project_id=project.id)


@project_membership_required
@require_POST
def setup_stop(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    try:
        request_stop(project=project, user=request.user)
    except SetupNoActiveTurnError as exc:
        messages.info(request, exc.message)
    except MachineSetupError as exc:
        messages.error(request, exc.message)
    return redirect("machine_setup:chat", project_id=project.id)


@project_membership_required
@require_POST
def setup_reset(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    reset_conversation(project=project, user=request.user)
    messages.success(request, "Started a new conversation.")
    return redirect("machine_setup:chat", project_id=project.id)
