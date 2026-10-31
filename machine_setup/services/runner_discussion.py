"""Use case: the Test Runner window reads and sends the discussion of a help case.

The discussion is the help request's ``case_help`` (or auto-fix) thread, the
same one the web help chat shows. ``get_runner_discussion`` gives its newest
display rows and whether a message can be sent now; ``send_from_runner``
sends a person's turn through ``open_help_chat`` (which reuses the thread or
opens it), so the turn has the gates, limits and Stop of any web send.

Identity and scoping follow ``runner_person_step_services``: only the
connection that owns the project, the case's open request only, and the
sender named only while the runner's email is an active writable member
(else "a person at the Test Runner"). Never imported by ``projects``.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from controller_client.protocol import (
    PERSON_DISCUSSION_MAX_MESSAGES,
    PERSON_DISCUSSION_MESSAGE_MAX_CHARS,
    PERSON_STEP_LABEL_MAX_CHARS,
    PERSON_STEP_RESULT_MESSAGE_MAX_CHARS,
    PERSON_STEP_URL_MAX_CHARS,
    DiscussionMessageKind,
    PersonDiscussionMessagePayload,
)
from machine_setup.exceptions import MachineSetupError, SetupTurnActiveError
from machine_setup.models import SetupConversation, SetupMessage, SetupMessageKind
from machine_setup.services.open_help_chat import open_help_chat
from machine_setup.services.send_gates import (
    machine_not_ready_refusal,
    run_and_update_refusal,
)
from machine_setup.subtask_types import RUN_SETUP_TASK_TOOL
from machine_setup.turn_state import live_turn_exists, person_turn_live
from notifications.services.site_url import absolute_url
from projects.exceptions import RunHelpError
from projects.models import Project
from projects.person_step_actor import PersonStepActor
from projects.runner_case_services import find_runner_help_case
from projects.runner_person_step_services import current_member, owns_project

# Discussion turns one project's Test Runner may send per window.
RUNNER_CHAT_LIMIT = 10
RUNNER_CHAT_WINDOW_SECONDS = 60

_NOT_OWNER = (
    "This Test Runner is no longer the project's connection. Write on Punk Hazard."
)
_THROTTLED = "Too many messages from this Test Runner. Wait a minute and try again."
_AGENT_NAME = "Machine setup agent"
_TOOL_NOTICE = "The agent ran: {title}"
_TOOL_NOTICE_FALLBACK = "The agent used {tool}."
# How much of a sub-task goal names it in a notice.
_TASK_TITLE_MAX_CHARS = 200


@dataclass(frozen=True)
class RunnerDiscussionView:
    """The discussion of a help case as the runner window shows it."""

    # Absolute thread page URL; "" before the thread opens or without SITE_URL.
    thread_url: str
    # A person's turn of this request is queued or running.
    turn_live: bool
    can_send: bool
    # Why a message cannot be sent now ("" when it can).
    send_blocker: str
    # Newest rows, oldest first.
    messages: tuple[PersonDiscussionMessagePayload, ...]


def get_runner_discussion(
    project: Project, help_request_id: int
) -> RunnerDiscussionView:
    """The request's discussion; no thread yet gives an empty one that can send.

    Queries: the thread (``llm_transcript`` deferred), its newest rows, the
    live turn checks and the send gates (the refusals ``send_setup_message``
    applies, checked without sending or closing stale turns).
    """
    thread = (
        SetupConversation.objects.filter(
            help_request_id=help_request_id,
            project=project,
            archived_at__isnull=True,
        )
        .only("id", "project_id")
        .first()
    )
    messages: tuple[PersonDiscussionMessagePayload, ...] = ()
    thread_url = ""
    if thread is not None:
        messages = _messages(thread.pk)
        path = reverse(
            "machine_setup:thread",
            kwargs={"project_id": project.pk, "conversation_id": thread.pk},
        )
        thread_url = (absolute_url(path) or "")[:PERSON_STEP_URL_MAX_CHARS]
    blocker = _send_blocker(project)
    return RunnerDiscussionView(
        thread_url=thread_url,
        turn_live=person_turn_live(help_request_id),
        can_send=not blocker,
        send_blocker=blocker[:PERSON_STEP_RESULT_MESSAGE_MAX_CHARS],
        messages=messages,
    )


def _send_blocker(project: Project) -> str:
    now = timezone.now()
    refusal: MachineSetupError | None = None
    if live_turn_exists(project.pk, now):
        refusal = SetupTurnActiveError()
    else:
        refusal = run_and_update_refusal(project, now) or machine_not_ready_refusal(
            project
        )
    return refusal.message if refusal is not None else ""


def _messages(thread_id: int) -> tuple[PersonDiscussionMessagePayload, ...]:
    """The thread's newest top-level rows, oldest first (one query).

    Sub-agent rows are left out; a tool row becomes a one-line notice.
    """
    rows = list(
        SetupMessage.objects.filter(conversation_id=thread_id, parent__isnull=True)
        .select_related("author")
        .only(
            "id",
            "kind",
            "text",
            "author_label",
            "tool_name",
            "tool_arguments",
            "created_at",
            "author__first_name",
            "author__last_name",
            "author__email",
        )
        .order_by("-id")[:PERSON_DISCUSSION_MAX_MESSAGES]
    )
    rows.reverse()
    return tuple(_message_payload(row) for row in rows)


def _message_payload(row: SetupMessage) -> PersonDiscussionMessagePayload:
    if row.kind == SetupMessageKind.TOOL:
        kind = DiscussionMessageKind.NOTICE
        text = _tool_notice(row)
        author = ""
    else:
        kind = DiscussionMessageKind(row.kind)
        text = row.text
        author = _author(row, kind)
    return PersonDiscussionMessagePayload(
        message_id=row.pk,
        kind=kind,
        author=author[:PERSON_STEP_LABEL_MAX_CHARS],
        text=text[:PERSON_DISCUSSION_MESSAGE_MAX_CHARS],
        created_at=row.created_at.isoformat(),
    )


def _author(row: SetupMessage, kind: DiscussionMessageKind) -> str:
    if kind is DiscussionMessageKind.ASSISTANT:
        return _AGENT_NAME
    if kind is not DiscussionMessageKind.USER:
        return ""
    if row.author_label:
        return row.author_label
    author = row.author
    if author is None:
        # The server's brief of the case.
        return "Case brief"
    return author.get_full_name() or author.email


def _tool_notice(row: SetupMessage) -> str:
    arguments = row.tool_arguments
    if row.tool_name == RUN_SETUP_TASK_TOOL and isinstance(arguments, dict):
        goal = arguments.get("goal")
        if isinstance(goal, str) and goal.strip():
            title = goal.strip().splitlines()[0][:_TASK_TITLE_MAX_CHARS]
            return _TOOL_NOTICE.format(title=title)
    return _TOOL_NOTICE_FALLBACK.format(tool=row.tool_name or "a tool")


def send_from_runner(
    *,
    project_id: int,
    channel_name: str,
    person_user_id: int | None,
    pivot_id: int,
    help_request_id: int,
    text: str,
) -> None:
    """Send a person's discussion turn about a help case from the Test Runner.

    Opens the request's help chat when it has none. Throttled to
    ``RUNNER_CHAT_LIMIT`` per minute per project. Raises
    ``MachineSetupError`` (user-safe) for a lost connection, the throttle, a
    stale card, or any refusal of the send (a turn is live, the machine is
    not ready, ...).
    """
    if not owns_project(project_id, channel_name):
        raise MachineSetupError(_NOT_OWNER)
    _check_chat_throttle(project_id)
    try:
        pivot = find_runner_help_case(
            project_id=project_id, pivot_id=pivot_id, help_request_id=help_request_id
        )
        project = Project.objects.get(pk=project_id)
        actor = PersonStepActor.runner(current_member(project_id, person_user_id))
        open_help_chat(project=project, pivot=pivot, actor=actor, text=text)
    except RunHelpError as exc:
        raise MachineSetupError(exc.message) from exc


def _check_chat_throttle(project_id: int) -> None:
    key = f"machine_setup:runner-chat:{project_id}"
    cache.add(key, 0, timeout=RUNNER_CHAT_WINDOW_SECONDS)
    try:
        attempts = cache.incr(key)
    except ValueError:
        # The key expired between add and incr: this is the window's first send.
        cache.add(key, 1, timeout=RUNNER_CHAT_WINDOW_SECONDS)
        attempts = 1
    if attempts > RUNNER_CHAT_LIMIT:
        raise MachineSetupError(_THROTTLED)
