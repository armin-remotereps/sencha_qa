"""Use case: a person opens (or re-opens) a run's case analysis thread.

One non-archived ``run_triage`` thread per run: an existing one is reused;
otherwise it is created with the brief (``run_triage``'s
``build_triage_brief``) as its first message. The person's message is then
sent through the ordinary ``send_setup_message``, so the turn gets the same
gates (no READY gate for this kind), limits and Stop as any setup turn.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction

from accounts.models import CustomUser
from agents.transcript import transcript_to_json
from agents.types import ChatMessage
from machine_setup.events import broadcast_message
from machine_setup.exceptions import MachineSetupError, TriageChatSendRefusedError
from machine_setup.models import (
    SetupConversation,
    SetupConversationKind,
    SetupMessage,
    SetupMessageKind,
)
from machine_setup.payloads import message_payload
from machine_setup.services.send_message import send_setup_message
from projects.models import Project, TestRun, TestRunStatus
from projects.person_step_actor import PersonStepActor
from projects.run_reopen_services import lock_project_and_run
from run_triage.services.agent_texts import count_analysable
from run_triage.services.triage_brief import build_triage_brief

logger = logging.getLogger(__name__)

NEW_THREAD_MESSAGE = "Analyze the cases of this run."
REANALYZE_MESSAGE = "Re-check the cases that changed since the last analysis."
DRAFT_RUN_MESSAGE = "A draft run has no results to analyse yet. Start it first."
RUN_GONE_MESSAGE = "This run no longer exists."
NOTHING_TO_ANALYSE_MESSAGE = (
    "Nothing to analyse yet: no case of this run is Failed, Blocked, Needs "
    "re-test or Cancelled with a result."
)


def find_triage_run(project: Project, test_run_id: int) -> TestRun | None:
    """The run of this project, or None (the view's 404)."""
    return (
        TestRun.objects.filter(pk=test_run_id, project=project)
        .only("id", "project_id")
        .first()
    )


def open_triage_chat(
    *, project: Project, test_run: TestRun, user: CustomUser, text: str
) -> SetupConversation:
    """The run's case analysis thread, with the person's message sent.

    ``test_run`` comes from ``find_triage_run``. Raises ``MachineSetupError``
    when the run is a draft, has nothing to analyse or more analysable cases
    than ``RUN_TRIAGE_MAX_CASES`` (nothing is created), and
    ``TriageChatSendRefusedError`` when the thread is kept but the message
    was refused (a turn is live, an agent-driven run is started, ...). An
    empty ``text`` asks for an analysis on a new thread and for a re-check
    of changed cases on an existing one.
    """
    thread, brief_row = _open_thread(project=project, test_run=test_run, user=user)
    if brief_row is not None:
        _broadcast_brief(brief_row)
    first = text.strip()
    if not first:
        first = NEW_THREAD_MESSAGE if brief_row is not None else REANALYZE_MESSAGE
    try:
        send_setup_message(
            project=project,
            actor=PersonStepActor.web(user),
            text=first,
            conversation=thread,
        )
    except MachineSetupError as exc:
        raise TriageChatSendRefusedError(
            exc.message, conversation_id=thread.pk
        ) from exc
    return thread


@transaction.atomic
def _open_thread(
    *, project: Project, test_run: TestRun, user: CustomUser
) -> tuple[SetupConversation, SetupMessage | None]:
    """The run's thread, and its brief row when it was created now."""
    # Same order as the run services (project, then run): a Reset or a
    # re-run cannot change the run between the checks and the thread.
    try:
        _, locked_run = lock_project_and_run(test_run.pk)
    except TestRun.DoesNotExist as exc:
        # Deleted since find_triage_run.
        raise MachineSetupError(RUN_GONE_MESSAGE) from exc
    if locked_run.status == TestRunStatus.WAITING:
        raise MachineSetupError(DRAFT_RUN_MESSAGE)
    analysable = count_analysable(locked_run.pk)
    if analysable == 0:
        raise MachineSetupError(NOTHING_TO_ANALYSE_MESSAGE)
    max_cases: int = settings.RUN_TRIAGE_MAX_CASES
    if analysable > max_cases:
        raise MachineSetupError(
            f"This run has {analysable:,} cases to analyse; the analysis takes "
            f"at most {max_cases:,}."
        )
    existing = (
        SetupConversation.objects.filter(
            project=project, triage_run=locked_run, archived_at__isnull=True
        )
        .defer("llm_transcript")
        .first()
    )
    if existing is not None:
        return existing, None
    brief = build_triage_brief(locked_run)
    thread = SetupConversation.objects.create(
        project=project,
        kind=SetupConversationKind.RUN_TRIAGE,
        triage_run=locked_run,
        created_by=user,
        llm_transcript=transcript_to_json((ChatMessage(role="user", content=brief),)),
    )
    # The brief as the server's display row: no author, no turn.
    brief_row = SetupMessage.objects.create(
        conversation=thread,
        turn=None,
        kind=SetupMessageKind.USER,
        author=None,
        text=brief,
    )
    return thread, brief_row


def _broadcast_brief(row: SetupMessage) -> None:
    """Announce the brief row, so an open main chat marks this thread as another one.

    Never raises: the page only misses a live update.
    """
    try:
        broadcast_message(
            row.conversation.project_id,
            message_payload(row, screenshots=[], attachments=[], knowledge_refs=[]),
        )
    except Exception:
        logger.exception(
            "Case analysis %s: brief broadcast failed", row.conversation_id
        )
