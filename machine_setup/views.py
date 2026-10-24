from __future__ import annotations

from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from accounts.types import AuthenticatedRequest
from machine_setup.exceptions import (
    MachineSetupError,
    SetupMachineNotReadyError,
    SetupNoActiveTurnError,
    SetupTurnActiveError,
)
from machine_setup.forms import SetupMessageForm, SetupThreadForm
from machine_setup.models import SetupConversation, SetupConversationKind
from machine_setup.services.case_auto_fix import settle_stale_auto_fix_turn
from machine_setup.services.chat_page import (
    OTHER_THREAD_TURN_MESSAGE,
    SetupPage,
    find_thread,
    get_setup_page,
    list_auto_fix_threads,
)
from machine_setup.services.reset_conversation import reset_conversation
from machine_setup.services.send_message import send_setup_message
from machine_setup.services.stop_turn import request_stop
from projects.decorators import project_membership_required
from projects.forms import form_error_text
from projects.models import Project


@project_membership_required
@require_GET
def setup_chat(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    # A killed worker's turn (an auto-fix one falls back to a person) is
    # closed when someone looks.
    settle_stale_auto_fix_turn(project.pk)
    page = get_setup_page(project, include_messages=False)
    return _render_chat(request, project, page, conversation=None)


@project_membership_required
@require_GET
def setup_thread(
    request: AuthenticatedRequest, project: Project, conversation_id: int
) -> HttpResponse:
    thread = find_thread(project, conversation_id)
    if thread is None:
        raise Http404("No such thread.")
    if thread.kind == SetupConversationKind.CHAT:
        return redirect("machine_setup:chat", project_id=project.id)
    settle_stale_auto_fix_turn(project.pk)
    page = get_setup_page(project, thread, include_messages=False)
    return _render_chat(request, project, page, conversation=thread)


def _render_chat(
    request: AuthenticatedRequest,
    project: Project,
    page: SetupPage,
    *,
    conversation: SetupConversation | None,
) -> HttpResponse:
    help_case_url = ""
    if page.thread is not None and page.thread.pivot_id is not None:
        help_case_url = reverse(
            "projects:test_run_case_detail",
            kwargs={
                "project_id": project.id,
                "test_run_id": page.thread.test_run_id,
                "pivot_id": page.thread.pivot_id,
            },
        )
    threads = list_auto_fix_threads(project)
    return render(
        request,
        "machine_setup/chat.html",
        {
            "project": project,
            "page": page,
            "conversation": conversation,
            "thread_kind": page.kind,
            "help_case_url": help_case_url,
            "threads": threads,
            "thread_ids": [thread.conversation_id for thread in threads],
            "visual_engine": page.visual_engine,
            "blocker_texts": {
                "machine_not_ready": SetupMachineNotReadyError.default_message,
                "turn_active": SetupTurnActiveError.default_message,
                "other_thread": OTHER_THREAD_TURN_MESSAGE,
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
        return _back_to_chat(project, _posted_thread_id(request))
    conversation_id: int | None = form.cleaned_data["conversation_id"]
    thread: SetupConversation | None = None
    if conversation_id is not None:
        thread = find_thread(project, conversation_id)
        if thread is None:
            raise Http404("No such thread.")
    try:
        send_setup_message(
            project=project,
            user=request.user,
            text=form.cleaned_data["text"],
            conversation=thread,
        )
    except MachineSetupError as exc:
        messages.error(request, exc.message)
    return _back_to_chat(project, conversation_id)


@project_membership_required
@require_POST
def setup_stop(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    try:
        request_stop(project=project, user=request.user)
    except SetupNoActiveTurnError as exc:
        messages.info(request, exc.message)
    except MachineSetupError as exc:
        messages.error(request, exc.message)
    return _back_to_chat(project, _posted_thread_id(request))


@project_membership_required
@require_POST
def setup_reset(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    reset_conversation(project=project, user=request.user)
    messages.success(request, "Started a new conversation.")
    return redirect("machine_setup:chat", project_id=project.id)


def _posted_thread_id(request: AuthenticatedRequest) -> int | None:
    form = SetupThreadForm(request.POST)
    if not form.is_valid():
        return None
    conversation_id: int | None = form.cleaned_data["conversation_id"]
    return conversation_id


def _back_to_chat(project: Project, conversation_id: int | None) -> HttpResponse:
    # A foreign or gone thread id 404s on the thread page itself.
    if conversation_id is None:
        return redirect("machine_setup:chat", project_id=project.id)
    return redirect(
        "machine_setup:thread",
        project_id=project.id,
        conversation_id=conversation_id,
    )
