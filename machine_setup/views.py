from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_GET, require_POST

from accounts.types import AuthenticatedRequest
from machine_setup.exceptions import (
    HelpChatSendRefusedError,
    MachineSetupError,
    SetupMachineNotReadyError,
    SetupNoActiveTurnError,
    SetupTurnActiveError,
)
from machine_setup.forms import OpenHelpChatForm, SetupMessageForm, SetupThreadForm
from machine_setup.models import SetupConversation, SetupConversationKind
from machine_setup.payloads import HelpThreadPayload
from machine_setup.services.case_auto_fix import settle_stale_auto_fix_turn
from machine_setup.services.chat_page import (
    OTHER_THREAD_TURN_MESSAGE,
    SetupPage,
    find_thread,
    get_setup_page,
    get_thread_help_panel,
    list_help_threads,
)
from machine_setup.services.document_download import find_document
from machine_setup.services.help_thread_state import help_panel_payload
from machine_setup.services.open_help_chat import find_help_case, open_help_chat
from machine_setup.services.reset_conversation import reset_conversation
from machine_setup.services.run_proposal_decision import (
    create_proposed_runs,
    discard_run_proposal,
)
from machine_setup.services.send_message import send_setup_message
from machine_setup.services.stop_turn import request_stop
from machine_setup.turn_state import MACHINE_SETUP_RUNNING_MESSAGE
from projects.case_help_services import HelpPanel
from projects.decorators import project_membership_required
from projects.exceptions import RunHelpError
from projects.forms import CaseHelpResolutionForm, form_error_text
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
    help_panel: HelpPanel | None = None
    help_form: CaseHelpResolutionForm | None = None
    help_state: HelpThreadPayload | None = None
    thread = page.thread
    if thread is not None and thread.pivot_id is not None:
        help_case_url = reverse(
            "projects:test_run_case_detail",
            kwargs={
                "project_id": project.id,
                "test_run_id": thread.test_run_id,
                "pivot_id": thread.pivot_id,
            },
        )
        help_panel = get_thread_help_panel(project, thread)
    if help_panel is not None and conversation is not None:
        help_state = help_panel_payload(conversation.pk, help_panel)
        draft = help_panel.pending_draft
        # The note is never pre-filled from the agent's suggestion: whatever
        # is submitted reaches the tester as the person's words.
        help_form = CaseHelpResolutionForm(
            initial={
                "use_plan_revision": True,
                "return_thread_id": conversation.pk,
                "plan_revision_id": draft.revision_id if draft is not None else None,
            }
        )
    threads = list_help_threads(project)
    return render(
        request,
        "machine_setup/chat.html",
        {
            "project": project,
            "page": page,
            "conversation": conversation,
            "thread_kind": page.kind,
            "help_case_url": help_case_url,
            "help_panel": help_panel,
            "help_form": help_form,
            "help_state": help_state,
            # The panel's gate that a live turn of this very thread raises;
            # the page tracks that turn itself.
            "help_turn_blocker": MACHINE_SETUP_RUNNING_MESSAGE,
            "threads": threads,
            "thread_ids": [thread.conversation_id for thread in threads],
            "visual_engine": page.visual_engine,
            "blocker_texts": {
                "machine_not_ready": SetupMachineNotReadyError.default_message,
                "turn_active": SetupTurnActiveError.default_message,
                "other_thread": OTHER_THREAD_TURN_MESSAGE,
            },
            "form": SetupMessageForm(),
            "document_max_bytes": settings.MACHINE_SETUP_DOCUMENT_MAX_BYTES,
            "document_url_template": reverse(
                "machine_setup:document",
                kwargs={"project_id": project.id, "document_id": 0},
            ),
            "run_proposal_create_url_template": reverse(
                "machine_setup:run_proposal_create",
                kwargs={"project_id": project.id, "proposal_id": 0},
            ),
            "run_proposal_discard_url_template": reverse(
                "machine_setup:run_proposal_discard",
                kwargs={"project_id": project.id, "proposal_id": 0},
            ),
            "active_tab": "machine_setup",
            "active_nav": "projects",
        },
    )


@project_membership_required
@require_POST
def help_chat_open(
    request: AuthenticatedRequest, project: Project, pivot_id: int
) -> HttpResponse:
    pivot = find_help_case(project, pivot_id)
    if pivot is None:
        raise Http404("No such test case.")
    case_url = reverse(
        "projects:test_run_case_detail",
        kwargs={
            "project_id": project.id,
            "test_run_id": pivot.test_run_id,
            "pivot_id": pivot.pk,
        },
    )
    form = OpenHelpChatForm(request.POST)
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return redirect(case_url)
    try:
        thread = open_help_chat(
            project=project,
            pivot=pivot,
            user=request.user,
            text=form.cleaned_data["text"],
        )
    except HelpChatSendRefusedError as exc:
        messages.error(request, exc.message)
        return _back_to_chat(project, exc.conversation_id)
    except (RunHelpError, MachineSetupError) as exc:
        messages.error(request, exc.message)
        return redirect(case_url)
    return _back_to_chat(project, thread.pk)


@project_membership_required
@require_POST
def setup_send(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    form = SetupMessageForm(request.POST, request.FILES)
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
            document=form.uploaded_document(),
        )
    except MachineSetupError as exc:
        messages.error(request, exc.message)
    return _back_to_chat(project, conversation_id)


@project_membership_required
@require_GET
def setup_document(
    request: AuthenticatedRequest, project: Project, document_id: int
) -> HttpResponse:
    document = find_document(project=project, document_id=document_id)
    if document is None:
        raise Http404("No such document.")
    response = HttpResponse(document.text, content_type="text/plain; charset=utf-8")
    disposition = content_disposition_header(
        as_attachment=True, filename=document.file_name
    )
    # Never None with as_attachment=True; narrowed for the type checker.
    if disposition is not None:
        response["Content-Disposition"] = disposition
    return response


@project_membership_required
@require_POST
def run_proposal_create(
    request: AuthenticatedRequest, project: Project, proposal_id: int
) -> HttpResponse:
    try:
        outcome = create_proposed_runs(
            project=project, proposal_id=proposal_id, user=request.user
        )
    except MachineSetupError as exc:
        messages.error(request, exc.message)
    else:
        count = outcome.created_count
        text = f"Created {count} draft run{'s' if count != 1 else ''}."
        if outcome.skipped_names:
            text += f" Skipped (no cases left): {', '.join(outcome.skipped_names)}."
        messages.success(request, text)
    return redirect("machine_setup:chat", project_id=project.id)


@project_membership_required
@require_POST
def run_proposal_discard(
    request: AuthenticatedRequest, project: Project, proposal_id: int
) -> HttpResponse:
    try:
        discard_run_proposal(
            project=project, proposal_id=proposal_id, user=request.user
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
