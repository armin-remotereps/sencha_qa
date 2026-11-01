from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Sequence
from typing import Any

from asgiref.sync import sync_to_async
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from controller_client.exceptions import ProtocolError
from controller_client.protocol import (
    MANUAL_CASE_ACTION_MAX_BYTES,
    MANUAL_CASE_SUBMIT_MAX_BYTES,
    MANUAL_SCREENSHOT_UPLOAD_MAX_BYTES,
    PERSON_CASE_ACTION_MAX_BYTES,
    PERSON_STEP_ACTION_MAX_BYTES,
    PERSON_STEP_QUESTION_MAX_BYTES,
    CasePauseActionResultPayload,
    ClientCapability,
    CloseCode,
    ErrorCode,
    ManualCaseActionKind,
    ManualCaseActionResultPayload,
    ManualCaseSubmitResultPayload,
    ManualRunStateMessage,
    ManualScreenshotAckPayload,
    MessageType,
    PersonCaseActionResultPayload,
    PersonCaseStatePayload,
    PersonHelpActionKind,
    PersonStepActionKind,
    PersonStepActionResultPayload,
    PersonStepQuestionAskPayload,
    PersonStepQuestionResultPayload,
    PersonStepStatePayload,
    parse_handshake_capabilities,
    parse_manual_case_submit_payload,
    parse_person_email,
    parse_update_in_flight_id,
    parse_update_status_payload,
    peek_manual_upload_ids,
    serialize_case_pause_action_result,
    serialize_manual_case_action_result,
    serialize_manual_case_submit_result,
    serialize_manual_run_state,
    serialize_manual_screenshot_ack,
    serialize_person_case_action_result,
    serialize_person_step_action_result,
    serialize_person_step_question_result,
    serialize_person_step_state,
)
from machine_setup.exceptions import MachineSetupError
from machine_setup.services.runner_discussion import send_from_runner
from projects.case_help_services import format_help_action_message
from projects.case_pause_policy import PAUSE_SECOND_MESSAGE, CasePauseOutcome
from projects.controller_authenticator import (
    ControllerAuthenticator,
    HandshakeMessageBuilder,
    HandshakeStatus,
)
from projects.controller_protocol import (
    ActionTypeRegistry,
    BaseActionEvent,
    BrowserClickActionEvent,
    BrowserDoubleClickActionEvent,
    BrowserDownloadActionEvent,
    BrowserGetElementsActionEvent,
    BrowserGetPageContentActionEvent,
    BrowserGetUrlActionEvent,
    BrowserHoverActionEvent,
    BrowserListDownloadsActionEvent,
    BrowserNavigateActionEvent,
    BrowserTakeScreenshotActionEvent,
    BrowserTypeActionEvent,
    CheckAppInstalledActionEvent,
    CleanupEnvironmentActionEvent,
    ClickActionEvent,
    CloseAppActionEvent,
    CloseTabActionEvent,
    ControllerMessageBuilder,
    DoubleClickActionEvent,
    DragActionEvent,
    HoverActionEvent,
    InspectPathsActionEvent,
    KeyPressActionEvent,
    LaunchAppActionEvent,
    RunCommandActionEvent,
    ScanDirectoryActionEvent,
    ScreenshotActionEvent,
    ScrollActionEvent,
    SendInputActionEvent,
    StartInteractiveCmdActionEvent,
    TerminateInteractiveCmdActionEvent,
    TypeTextActionEvent,
    UpdateRunnerEvent,
    WaitForCommandActionEvent,
)
from projects.controller_reply_tracker import ReplyTracker
from projects.exceptions import (
    CasePauseError,
    ManualRunError,
    ManualShotRejectedError,
    ManualSubmissionDroppedError,
    PersonStepError,
    PersonStepQuestionError,
    RunHelpError,
)
from projects.forms import (
    RunnerCasePauseForm,
    RunnerChatSendForm,
    RunnerHelpActionForm,
    RunnerManualCaseActionForm,
    RunnerManualStepQuestionForm,
    RunnerPersonStepActionForm,
    RunnerPersonStepQuestionForm,
    form_error_text,
)
from projects.models import HelpContinueMode, Project
from projects.person_step_services import PersonStepActionResult
from projects.runner_case_pause_services import pause_from_runner
from projects.runner_help_action_services import (
    RunnerHelpAction,
    resolve_help_from_runner,
)
from projects.runner_manual_services import (
    act_from_runner,
    ask_manual_from_runner,
    submit_from_runner,
    upload_from_runner,
)
from projects.runner_person_step_services import (
    RunnerPersonStepAnswer,
    answer_from_runner,
    ask_from_runner,
    resolve_runner_person,
)
from projects.runner_update_progress import (
    record_runner_update_status,
    settle_runner_update_on_handshake,
)
from projects.runner_window_state import (
    RunnerWindowFeatures,
    get_runner_manual_state,
    get_runner_window_state,
)
from projects.services import broadcast_agent_status, release_agent_connection

logger = logging.getLogger(__name__)

# What the Test Runner window says after an answer was saved and the run
# continues (the web card's flash texts).
_PERSON_STEP_DONE: dict[PersonStepActionKind, str] = {
    PersonStepActionKind.REPORT: "Reported. The agent is checking it.",
    PersonStepActionKind.OVERRULE: "Overruled. The step is recorded as passed.",
    PersonStepActionKind.FAIL: "Marked the case failed. The run continues.",
    PersonStepActionKind.BLOCK: "Marked the case blocked. The run continues.",
}
_PERSON_STEPS_UNSUPPORTED = "This Test Runner did not offer person steps."
_PERSON_STEP_TOO_LARGE = "The answer is too long."
_PERSON_STEP_FAILED = "The answer could not be saved. Answer on Punk Hazard."
_PERSON_STEP_QUESTION_UNSUPPORTED = "This Test Runner did not offer Ask AI."
_PERSON_STEP_QUESTION_TOO_LARGE = "The question is too long."
_PERSON_STEP_QUESTION_FAILED = "The question could not be sent. Ask on Punk Hazard."
_CASE_ACTION_UNSUPPORTED = "This Test Runner did not offer this action."
_CASE_ACTION_TOO_LARGE = "The message is too long."
_CASE_ACTION_FAILED = "That did not work. Try it on Punk Hazard."
_PAUSE_DONE: dict[CasePauseOutcome, str] = {
    CasePauseOutcome.REQUESTED: (
        "Pausing: the agent stops at its next action; the discussion opens "
        "when it has stopped."
    ),
    CasePauseOutcome.ALREADY_REQUESTED: "Already pausing.",
    CasePauseOutcome.PAUSED: "Paused; opening the discussion.",
}
_HELP_ACTION_VERB: dict[PersonHelpActionKind, str] = {
    PersonHelpActionKind.CONTINUE: "Continuing",
    PersonHelpActionKind.MARK_FAILED: "Marked the case failed",
    PersonHelpActionKind.MARK_BLOCKED: "Marked the case blocked",
}
_CHAT_SENT = "Sent. The agent is answering."
_MANUAL_UNSUPPORTED = "This Test Runner did not offer manual testing."
_MANUAL_TOO_LARGE = "The message is too large."
_MANUAL_ACTION_FAILED = "That did not work. Try again in a moment."
_MANUAL_SUBMIT_FAILED = "The result could not be saved. It is kept and sent again."
_MANUAL_SUBMITTED = "Submitted. Screenshots upload in the background."
_MANUAL_UPLOAD_FAILED = "The screenshot could not be saved. It is sent again."
# Nudges within this many seconds of a push wait for one trailing push.
_PUSH_COALESCE_SECONDS = 1.0
# Longest ``request_id`` echoed back; a longer one gets a fresh id.
_MAX_REQUEST_ID_LENGTH = 128
# Longest raw ``submission_id`` read back for a refused hand-in.
_MAX_SUBMISSION_ID_LENGTH = 64


class ControllerConsumer(AsyncWebsocketConsumer):
    _authenticated: bool
    _project: Project | None
    _reply_tracker: ReplyTracker
    _authenticator: ControllerAuthenticator
    _message_builder: ControllerMessageBuilder
    # The runner shows person steps (it advertised PERSON_STEPS_V1).
    _person_steps_enabled: bool
    # The runner shows how-to lists and Ask AI (PERSON_STEP_ASSIST_V1).
    _assist_enabled: bool
    # The runner takes up to 12 how-to items with sources
    # (PERSON_STEP_GUIDE_V1, only together with PERSON_STEP_ASSIST_V1).
    _guide_enabled: bool
    # The runner shows the test case steps a step covers
    # (PERSON_STEP_CASE_STEPS_V1).
    _case_steps_enabled: bool
    # The active member the handshake's ``person_email`` named; re-checked
    # when an answer arrives. The email itself is never kept.
    _person_user_id: int | None
    # The window's v2 features (PERSON_CASE_CONTEXT_V1, PERSON_DISCUSSION_V1,
    # CASE_PAUSE_V1), each only together with PERSON_STEPS_V1.
    _case_context_enabled: bool
    _discussion_enabled: bool
    _pause_enabled: bool
    # Manual runs in the window (MANUAL_RUN_V1, only with PERSON_STEPS_V1).
    _manual_enabled: bool
    # Everything the handshake advertised (passed to the manual snapshot).
    _capabilities: frozenset[str]
    # The last ``manual_run_state`` sent; None before the first one.
    _manual_state_sent: ManualRunStateMessage | None
    # The last ``person_step_state`` sent (steps, cases); None before the
    # first one.
    _person_steps_sent: (
        tuple[
            tuple[PersonStepStatePayload, ...],
            tuple[PersonCaseStatePayload, ...] | None,
        ]
        | None
    )
    # Loop time of the last snapshot read, and the one trailing push nudges
    # wait for (coalescing bursts of chat and tool rows).
    _last_push_at: float
    _trailing_push: asyncio.Task[None] | None
    # Serialises snapshot pushes, so an older snapshot is never sent last.
    _push_lock: asyncio.Lock

    async def connect(self) -> None:
        self._authenticated = False
        self._project = None
        self._person_steps_enabled = False
        self._assist_enabled = False
        self._guide_enabled = False
        self._case_steps_enabled = False
        self._case_context_enabled = False
        self._discussion_enabled = False
        self._pause_enabled = False
        self._manual_enabled = False
        self._capabilities = frozenset()
        self._manual_state_sent = None
        self._person_user_id = None
        self._person_steps_sent = None
        self._last_push_at = 0.0
        self._trailing_push = None
        self._push_lock = asyncio.Lock()
        self._reply_tracker = ReplyTracker(self.channel_layer)
        self._authenticator = ControllerAuthenticator()
        self._message_builder = ControllerMessageBuilder(ActionTypeRegistry())
        await self.accept()

    async def receive(
        self, text_data: str | None = None, bytes_data: bytes | None = None
    ) -> None:
        if text_data is None:
            return

        try:
            data: dict[str, Any] = json.loads(text_data)
        except json.JSONDecodeError:
            await self._send_error("Invalid JSON")
            return

        if not self._authenticated:
            await self._handle_unauthenticated_message(data)
            return

        await self._route_authenticated_message(data, len(text_data))

    async def disconnect(self, close_code: int) -> None:
        if self._trailing_push is not None:
            self._trailing_push.cancel()
            self._trailing_push = None
        if self._authenticated and self._project is not None:
            await self._cleanup_connection()

    async def _handle_unauthenticated_message(self, data: dict[str, Any]) -> None:
        msg_type: str = data.get("type", "")
        request_id: str = data.get("request_id", "")

        if msg_type == "handshake":
            await self._handle_handshake(data, request_id)
        else:
            await self._send_error(
                "Not authenticated. Send handshake first.", request_id
            )
            await self.close()

    async def _route_authenticated_message(
        self, data: dict[str, Any], raw_length: int
    ) -> None:
        msg_type: str = data.get("type", "")
        request_id: str = data.get("request_id", "")

        # Unknown types fall through silently, including the
        # ``omniparser_status`` reports a 0.5.x Test Runner still sends.
        if msg_type == "pong":
            return

        if msg_type == MessageType.UPDATE_STATUS:
            await self._handle_update_status(data)
            return

        if msg_type == MessageType.PERSON_STEP_ACTION:
            await self._handle_person_step_action(data, raw_length)
            return

        if msg_type == MessageType.PERSON_STEP_QUESTION:
            await self._handle_person_step_question(data, raw_length)
            return

        if msg_type == MessageType.CASE_PAUSE_ACTION:
            await self._handle_case_pause_action(data, raw_length)
            return

        if msg_type in (MessageType.PERSON_CHAT_SEND, MessageType.PERSON_HELP_ACTION):
            await self._handle_person_case_action(msg_type, data, raw_length)
            return

        if msg_type == MessageType.MANUAL_CASE_ACTION:
            await self._handle_manual_case_action(data, raw_length)
            return

        if msg_type == MessageType.MANUAL_CASE_SUBMIT:
            await self._handle_manual_case_submit(data, raw_length)
            return

        if msg_type == MessageType.MANUAL_SCREENSHOT_UPLOAD:
            await self._handle_manual_screenshot_upload(data, raw_length)
            return

        handlers = {
            "action_result": self._reply_tracker.send_action_result,
            "screenshot_response": self._reply_tracker.send_screenshot_result,
            "command_output": self._reply_tracker.send_command_output,
            "command_result": self._reply_tracker.send_command_result,
            "browser_content_result": self._reply_tracker.send_browser_content_result,
            "interactive_output": self._reply_tracker.send_interactive_output,
            MessageType.ARTIFACT_SCAN_RESULT: (
                self._reply_tracker.send_artifact_scan_result
            ),
            "error": self._reply_tracker.send_error,
        }

        handler = handlers.get(msg_type)
        if handler:
            await handler(request_id, data)

    async def _handle_update_status(self, data: dict[str, Any]) -> None:
        if self._project is None:
            return
        try:
            status = parse_update_status_payload(data)
        except ProtocolError as exc:
            logger.warning(
                "Ignoring malformed update_status for project %s: %s",
                self._project.id,
                exc,
            )
            return
        await sync_to_async(record_runner_update_status)(
            self._project.id, status.update_id, status.phase, status.detail
        )

    async def _handle_handshake(self, data: dict[str, Any], request_id: str) -> None:
        api_key: str = data.get("api_key", "")
        system_info: dict[str, Any] = data.get("system_info", {})
        client_version: str = data.get("client_version", "")

        try:
            capabilities = parse_handshake_capabilities(data)
            update_in_flight_id = parse_update_in_flight_id(data)
            person_email = parse_person_email(data)
        except ProtocolError as exc:
            await self._send_handshake_ack(
                "error", f"Invalid handshake: {exc}", request_id
            )
            await self.close()
            return

        result = await self._authenticator.authenticate_handshake(
            api_key, system_info, client_version, capabilities
        )

        if not result.success or result.project is None:
            logger.warning(
                "Controller handshake rejected (%s, client_version=%s): %s",
                result.status,
                client_version or "unknown",
                result.reason,
            )
            await self._send_handshake_ack(result.status, result.reason, request_id)
            await self.close()
            return

        # Never echoed and never an error: the runner does not learn whether
        # the email belongs to a member.
        self._person_user_id = await sync_to_async(resolve_runner_person)(
            result.project.id, person_email
        )
        self._person_steps_enabled = (
            ClientCapability.PERSON_STEPS_V1.value in capabilities
        )
        # Advertised only together with PERSON_STEPS_V1 (the window).
        self._assist_enabled = (
            self._person_steps_enabled
            and ClientCapability.PERSON_STEP_ASSIST_V1.value in capabilities
        )
        # Advertised only together with PERSON_STEP_ASSIST_V1.
        self._guide_enabled = (
            self._assist_enabled
            and ClientCapability.PERSON_STEP_GUIDE_V1.value in capabilities
        )
        # Advertised only together with PERSON_STEPS_V1 (the window).
        self._case_steps_enabled = (
            self._person_steps_enabled
            and ClientCapability.PERSON_STEP_CASE_STEPS_V1.value in capabilities
        )
        # Window v2, each advertised only together with PERSON_STEPS_V1.
        self._case_context_enabled = (
            self._person_steps_enabled
            and ClientCapability.PERSON_CASE_CONTEXT_V1.value in capabilities
        )
        self._discussion_enabled = (
            self._case_context_enabled
            and ClientCapability.PERSON_DISCUSSION_V1.value in capabilities
        )
        self._pause_enabled = (
            self._case_context_enabled
            and ClientCapability.CASE_PAUSE_V1.value in capabilities
        )
        # Advertised only together with PERSON_STEPS_V1 (the window).
        self._manual_enabled = (
            self._person_steps_enabled
            and ClientCapability.MANUAL_RUN_V1.value in capabilities
        )
        self._capabilities = frozenset(capabilities)
        await self._establish_authenticated_connection(
            result.project,
            system_info,
            client_version,
            capabilities,
            update_in_flight_id,
            request_id,
        )

    async def _establish_authenticated_connection(
        self,
        project: Project,
        system_info: dict[str, Any],
        client_version: str,
        capabilities: Sequence[str],
        update_in_flight_id: str | None,
        request_id: str,
    ) -> None:
        self._authenticated = True
        self._project = project

        # Join the group before ownership becomes visible in the DB, so an
        # action dispatched to the new owner always reaches this consumer.
        group_name = self._controller_group_name(project.id)
        await self.channel_layer.group_add(group_name, self.channel_name)
        previous_channel_name = await self._authenticator.claim_connection(
            project, self.channel_name, system_info, client_version, capabilities
        )
        # A runner started by a self-update reports the target version here;
        # an old runner reconnecting mid-update reports the update it is
        # installing; any other runner ends the in-flight update as lost.
        # Settling before the old runner is superseded keeps the panel from
        # showing a lost update in between.
        await sync_to_async(settle_runner_update_on_handshake)(
            project.id, client_version, update_in_flight_id
        )
        if previous_channel_name:
            # Drop the replaced connection from the group even if its consumer
            # is gone (e.g. Daphne restarted), then ask it to close if alive.
            await self.channel_layer.group_discard(group_name, previous_channel_name)
            await self.channel_layer.send(
                previous_channel_name, {"type": "controller.superseded"}
            )
        await self._authenticator.broadcast_status(project)
        await self._send_handshake_ack(
            status="ok",
            message="Connected",
            request_id=request_id,
            project_id=str(project.id),
            project_name=project.name,
        )
        # Covers a reconnect or a runner restart while a step waits.
        await self._push_person_steps()

    async def _cleanup_connection(self) -> None:
        if self._project is None:
            return

        await self.channel_layer.group_discard(
            self._controller_group_name(self._project.id), self.channel_name
        )
        # Only the current owner clears the connection; a superseded consumer
        # releases nothing. A running test waits for the runner to reconnect.
        released = await sync_to_async(release_agent_connection)(
            self._project.id, self.channel_name
        )
        # Wake the waiters only after ownership is released, so their
        # wait_for_controller never hands this dead channel back to them.
        await self._reply_tracker.fail_pending_replies(
            ErrorCode.CONTROLLER_DISCONNECTED, "Test Runner connection lost"
        )
        if released is not None:
            await sync_to_async(broadcast_agent_status)(released)

    async def controller_superseded(self, event: dict[str, Any]) -> None:
        await self.close(code=int(CloseCode.SUPERSEDED))

    async def controller_force_disconnect(self, event: dict[str, Any]) -> None:
        await self.close(code=int(CloseCode.FORCE_DISCONNECTED))

    async def controller_person_steps_changed(self, event: dict[str, Any]) -> None:
        """A nudge: push now, or once a second at most (one trailing push)."""
        if not self._person_steps_enabled or self._trailing_push is not None:
            return
        wait = (
            self._last_push_at
            + _PUSH_COALESCE_SECONDS
            - asyncio.get_running_loop().time()
        )
        if wait <= 0:
            await self._push_person_steps()
            return
        self._trailing_push = asyncio.ensure_future(self._trailing_push_after(wait))

    async def _trailing_push_after(self, wait: float) -> None:
        try:
            await asyncio.sleep(wait)
        finally:
            self._trailing_push = None
        try:
            await self._push_person_steps()
        except Exception:
            # Nobody awaits this task: log here (e.g. the socket closed).
            logger.exception("Trailing person step push failed")

    async def _push_person_steps(self, *, force: bool = False) -> None:
        """Send the window's snapshot when it changed since the last send.

        Only to a runner that advertised PERSON_STEPS_V1; ``cases`` only to one
        with PERSON_CASE_CONTEXT_V1; and the ``manual_run_state`` only to one
        with MANUAL_RUN_V1. ``force`` resends an unchanged snapshot (after a
        refused answer the window must re-sync).
        """
        if not self._person_steps_enabled or self._project is None:
            return
        async with self._push_lock:
            await self._push_snapshot(self._project.id, force=force)
            if self._manual_enabled:
                await self._send_manual_state(self._project, force=force)

    async def _push_snapshot(self, project_id: int, *, force: bool) -> None:
        self._last_push_at = asyncio.get_running_loop().time()
        try:
            state = await sync_to_async(get_runner_window_state)(
                project_id,
                RunnerWindowFeatures(
                    assist=self._assist_enabled,
                    guide=self._guide_enabled,
                    case_steps=self._case_steps_enabled,
                    case_context=self._case_context_enabled,
                    discussion=self._discussion_enabled,
                    pause=self._pause_enabled,
                ),
            )
        except Exception:
            logger.exception("Reading person steps failed for project %s", project_id)
            return
        snapshot = (state.steps, state.cases)
        # The first push of a connection always goes out (even empty), so a
        # reconnected runner drops a step answered meanwhile.
        if not force and snapshot == self._person_steps_sent:
            return
        self._person_steps_sent = snapshot
        await self.send(
            text_data=serialize_person_step_state(
                state.steps,
                questions_enabled=self._assist_enabled,
                cases=state.cases,
            )
        )

    async def _push_manual_state(self, *, force: bool = False) -> None:
        """Send only the Manual run snapshot (after a manual message)."""
        if not self._manual_enabled or self._project is None:
            return
        async with self._push_lock:
            await self._send_manual_state(self._project, force=force)

    async def _send_manual_state(self, project: Project, *, force: bool) -> None:
        """Send ``manual_run_state`` to a MANUAL_RUN_V1 runner when it changed.

        The first send of a connection always goes out (even empty), so a
        reconnected runner learns its pending uploads and drops a stale claim.
        """
        try:
            state = await database_sync_to_async(get_runner_manual_state)(
                project, self._capabilities
            )
        except Exception:
            logger.exception("Reading the manual run failed for project %s", project.id)
            return
        if not force and state == self._manual_state_sent:
            return
        self._manual_state_sent = state
        await self.send(text_data=serialize_manual_run_state(state))

    async def _handle_case_pause_action(
        self, data: dict[str, Any], raw_length: int
    ) -> None:
        request_id = _echoed_request_id(data)
        result = await self._pause_case(data, raw_length)
        await self.send(
            text_data=serialize_case_pause_action_result(result, request_id)
        )
        await self._push_person_steps(force=True)

    async def _pause_case(
        self, data: dict[str, Any], raw_length: int
    ) -> CasePauseActionResultPayload:
        if not self._pause_enabled or self._project is None:
            return _pause_refused(_CASE_ACTION_UNSUPPORTED)
        if raw_length > PERSON_CASE_ACTION_MAX_BYTES:
            return _pause_refused(_CASE_ACTION_TOO_LARGE)
        form = RunnerCasePauseForm(
            {field: data.get(field) for field in RunnerCasePauseForm.base_fields}
        )
        if not form.is_valid():
            return _pause_refused(form_error_text(form))
        message: str = form.cleaned_data["message"]
        try:
            outcome = await sync_to_async(pause_from_runner)(
                project_id=self._project.id,
                channel_name=self.channel_name,
                person_user_id=self._person_user_id,
                pivot_id=form.cleaned_data["pivot_id"],
                message=message,
            )
        except CasePauseError as exc:
            return _pause_refused(exc.message)
        except Exception:
            logger.exception("Case pause failed for project %s", self._project.id)
            return _pause_refused(_CASE_ACTION_FAILED)
        text = _PAUSE_DONE[outcome]
        if outcome == CasePauseOutcome.ALREADY_REQUESTED and message:
            text = f"{text} {PAUSE_SECOND_MESSAGE}"
        return CasePauseActionResultPayload(
            ok=True, outcome=outcome.value, message=text
        )

    async def _handle_person_case_action(
        self, msg_type: str, data: dict[str, Any], raw_length: int
    ) -> None:
        request_id = _echoed_request_id(data)
        if not self._discussion_enabled or self._project is None:
            result = _case_refused(_CASE_ACTION_UNSUPPORTED)
        elif raw_length > PERSON_CASE_ACTION_MAX_BYTES:
            result = _case_refused(_CASE_ACTION_TOO_LARGE)
        elif msg_type == MessageType.PERSON_CHAT_SEND:
            result = await self._send_chat(self._project.id, data)
        else:
            result = await self._resolve_help(self._project.id, data)
        await self.send(
            text_data=serialize_person_case_action_result(result, request_id)
        )
        await self._push_person_steps(force=True)

    async def _send_chat(
        self, project_id: int, data: dict[str, Any]
    ) -> PersonCaseActionResultPayload:
        form = RunnerChatSendForm(
            {field: data.get(field) for field in RunnerChatSendForm.base_fields}
        )
        if not form.is_valid():
            return _case_refused(form_error_text(form))
        try:
            await sync_to_async(send_from_runner)(
                project_id=project_id,
                channel_name=self.channel_name,
                person_user_id=self._person_user_id,
                pivot_id=form.cleaned_data["pivot_id"],
                help_request_id=form.cleaned_data["help_request_id"],
                text=form.cleaned_data["text"],
            )
        except MachineSetupError as exc:
            return _case_refused(exc.message)
        except Exception:
            logger.exception("Runner chat send failed for project %s", project_id)
            return _case_refused(_CASE_ACTION_FAILED)
        return PersonCaseActionResultPayload(ok=True, message=_CHAT_SENT)

    async def _resolve_help(
        self, project_id: int, data: dict[str, Any]
    ) -> PersonCaseActionResultPayload:
        form = RunnerHelpActionForm(
            {field: data.get(field) for field in RunnerHelpActionForm.base_fields}
        )
        if not form.is_valid():
            return _case_refused(form_error_text(form))
        kind = PersonHelpActionKind(form.cleaned_data["action"])
        raw_mode: str = form.cleaned_data["continue_mode"]
        action = RunnerHelpAction(
            pivot_id=form.cleaned_data["pivot_id"],
            help_request_id=form.cleaned_data["help_request_id"],
            action=kind,
            note=form.cleaned_data["note"],
            continue_mode=HelpContinueMode(raw_mode) if raw_mode else None,
            plan_revision_id=form.cleaned_data["plan_revision_id"],
            use_plan_revision=form.cleaned_data["use_plan_revision"],
        )
        try:
            result = await sync_to_async(resolve_help_from_runner)(
                project_id=project_id,
                channel_name=self.channel_name,
                person_user_id=self._person_user_id,
                action=action,
            )
        except RunHelpError as exc:
            return _case_refused(exc.message)
        except Exception:
            logger.exception("Runner help action failed for project %s", project_id)
            return _case_refused(_CASE_ACTION_FAILED)
        return PersonCaseActionResultPayload(
            ok=True,
            message=format_help_action_message(_HELP_ACTION_VERB[kind], result),
        )

    async def _handle_person_step_action(
        self, data: dict[str, Any], raw_length: int
    ) -> None:
        request_id = _echoed_request_id(data)
        result = await self._answer_person_step(data, raw_length)
        await self.send(
            text_data=serialize_person_step_action_result(result, request_id)
        )
        if not result.ok:
            await self._push_person_steps(force=True)

    async def _answer_person_step(
        self, data: dict[str, Any], raw_length: int
    ) -> PersonStepActionResultPayload:
        if not self._person_steps_enabled or self._project is None:
            return _refused(_PERSON_STEPS_UNSUPPORTED)
        if raw_length > PERSON_STEP_ACTION_MAX_BYTES:
            return _refused(_PERSON_STEP_TOO_LARGE)
        form = RunnerPersonStepActionForm(
            {field: data.get(field) for field in RunnerPersonStepActionForm.base_fields}
        )
        if not form.is_valid():
            return _refused(form_error_text(form))
        answer = RunnerPersonStepAnswer(
            action=PersonStepActionKind(form.cleaned_data["action"]),
            person_step_id=form.cleaned_data["person_step_id"],
            round=form.cleaned_data["round"],
            outcome=form.cleaned_data["outcome"],
            note=form.cleaned_data["note"],
            output=form.cleaned_data["output"],
        )
        try:
            outcome = await sync_to_async(answer_from_runner)(
                project_id=self._project.id,
                channel_name=self.channel_name,
                person_user_id=self._person_user_id,
                action=answer,
            )
        except PersonStepError as exc:
            return _refused(exc.message)
        except Exception:
            logger.exception(
                "Person step answer failed for project %s", self._project.id
            )
            return _refused(_PERSON_STEP_FAILED)
        return _answered(answer.action, outcome)

    async def _handle_person_step_question(
        self, data: dict[str, Any], raw_length: int
    ) -> None:
        request_id = _echoed_request_id(data)
        if _is_manual_question(data):
            result = await self._ask_manual_step(data, raw_length)
        else:
            result = await self._ask_person_step(data, raw_length)
        await self.send(
            text_data=serialize_person_step_question_result(result, request_id)
        )
        if not result.ok:
            await self._push_person_steps(force=True)

    async def _ask_person_step(
        self, data: dict[str, Any], raw_length: int
    ) -> PersonStepQuestionResultPayload:
        if not self._assist_enabled or self._project is None:
            return _question_refused(_PERSON_STEP_QUESTION_UNSUPPORTED)
        if raw_length > PERSON_STEP_QUESTION_MAX_BYTES:
            return _question_refused(_PERSON_STEP_QUESTION_TOO_LARGE)
        form = RunnerPersonStepQuestionForm(
            {
                field: data.get(field)
                for field in RunnerPersonStepQuestionForm.base_fields
            }
        )
        if not form.is_valid():
            return _question_refused(form_error_text(form))
        question = PersonStepQuestionAskPayload(
            person_step_id=form.cleaned_data["person_step_id"],
            round=form.cleaned_data["round"],
            question=form.cleaned_data["question"],
        )
        try:
            await sync_to_async(ask_from_runner)(
                project_id=self._project.id,
                channel_name=self.channel_name,
                person_id=self._person_user_id,
                question=question,
            )
        except PersonStepQuestionError as exc:
            return _question_refused(exc.message)
        except Exception:
            logger.exception(
                "Person step question failed for project %s", self._project.id
            )
            return _question_refused(_PERSON_STEP_QUESTION_FAILED)
        # The answer arrives in a later ``person_step_state``.
        return PersonStepQuestionResultPayload(ok=True, message="")

    async def _ask_manual_step(
        self, data: dict[str, Any], raw_length: int
    ) -> PersonStepQuestionResultPayload:
        """The manual form of ``person_step_question`` (``pivot_id`` set)."""
        if not (self._manual_enabled and self._assist_enabled) or self._project is None:
            return _question_refused(_PERSON_STEP_QUESTION_UNSUPPORTED)
        if raw_length > PERSON_STEP_QUESTION_MAX_BYTES:
            return _question_refused(_PERSON_STEP_QUESTION_TOO_LARGE)
        form = RunnerManualStepQuestionForm(
            {
                field: data.get(field)
                for field in RunnerManualStepQuestionForm.base_fields
            }
        )
        if not form.is_valid():
            return _question_refused(form_error_text(form))
        try:
            await database_sync_to_async(ask_manual_from_runner)(
                project=self._project,
                channel_name=self.channel_name,
                person_user_id=self._person_user_id,
                pivot_id=form.cleaned_data["pivot_id"],
                claim_token=form.cleaned_data["claim_token"],
                step_num=form.cleaned_data["step_num"],
                question=form.cleaned_data["question"],
            )
        except (ManualRunError, PersonStepQuestionError) as exc:
            return _question_refused(exc.message)
        except Exception:
            logger.exception(
                "Manual step question failed for project %s", self._project.id
            )
            return _question_refused(_PERSON_STEP_QUESTION_FAILED)
        # The answer arrives in a later ``manual_run_state``.
        return PersonStepQuestionResultPayload(ok=True, message="")

    async def _handle_manual_case_action(
        self, data: dict[str, Any], raw_length: int
    ) -> None:
        request_id = _echoed_request_id(data)
        result = await self._act_on_manual_case(data, raw_length)
        await self.send(
            text_data=serialize_manual_case_action_result(result, request_id)
        )
        # A success nudges on commit; only a refusal re-syncs the window here.
        if not result.ok:
            await self._push_manual_state(force=True)

    async def _act_on_manual_case(
        self, data: dict[str, Any], raw_length: int
    ) -> ManualCaseActionResultPayload:
        if not self._manual_enabled or self._project is None:
            return _manual_action_refused(_MANUAL_UNSUPPORTED)
        if raw_length > MANUAL_CASE_ACTION_MAX_BYTES:
            return _manual_action_refused(_MANUAL_TOO_LARGE)
        form = RunnerManualCaseActionForm(
            {field: data.get(field) for field in RunnerManualCaseActionForm.base_fields}
        )
        if not form.is_valid():
            return _manual_action_refused(form_error_text(form))
        try:
            message = await database_sync_to_async(act_from_runner)(
                project=self._project,
                channel_name=self.channel_name,
                person_user_id=self._person_user_id,
                action=ManualCaseActionKind(form.cleaned_data["action"]),
                pivot_id=form.cleaned_data["pivot_id"],
                claim_token=form.cleaned_data["claim_token"],
            )
        except ManualRunError as exc:
            return _manual_action_refused(exc.message)
        except Exception:
            logger.exception(
                "Manual case action failed for project %s", self._project.id
            )
            return _manual_action_refused(_MANUAL_ACTION_FAILED)
        return ManualCaseActionResultPayload(ok=True, message=message)

    async def _handle_manual_case_submit(
        self, data: dict[str, Any], raw_length: int
    ) -> None:
        request_id = _echoed_request_id(data)
        result = await self._submit_manual_case(data, raw_length)
        await self.send(
            text_data=serialize_manual_case_submit_result(result, request_id)
        )
        # A success nudges on commit; only a refusal re-syncs the window here.
        if not result.ok:
            await self._push_manual_state(force=True)

    async def _submit_manual_case(
        self, data: dict[str, Any], raw_length: int
    ) -> ManualCaseSubmitResultPayload:
        if not self._manual_enabled or self._project is None:
            return _manual_submit_refused(_MANUAL_UNSUPPORTED, "")
        if raw_length > MANUAL_CASE_SUBMIT_MAX_BYTES:
            return _manual_submit_refused(_MANUAL_TOO_LARGE, "")
        # Nested lists: the protocol parser is the form here (structure, caps,
        # note rules); the service checks the claim and the stored plan.
        try:
            payload = parse_manual_case_submit_payload(data)
        except ProtocolError as exc:
            # The same hand-in can never pass: the runner deletes it.
            return _manual_submit_refused(
                f"The result was refused: {exc}",
                _peek_submission_id(data),
                drop=True,
            )
        submission_id = payload.submission_id
        try:
            missing = await database_sync_to_async(submit_from_runner)(
                project=self._project,
                channel_name=self.channel_name,
                person_user_id=self._person_user_id,
                payload=payload,
            )
        except ManualSubmissionDroppedError as exc:
            return _manual_submit_refused(exc.message, submission_id, drop=True)
        except ManualRunError as exc:
            return _manual_submit_refused(exc.message, submission_id)
        except Exception:
            logger.exception(
                "Manual case submit failed for project %s", self._project.id
            )
            return _manual_submit_refused(_MANUAL_SUBMIT_FAILED, submission_id)
        return ManualCaseSubmitResultPayload(
            ok=True,
            message=_MANUAL_SUBMITTED,
            submission_id=submission_id,
            missing=missing,
        )

    async def _handle_manual_screenshot_upload(
        self, data: dict[str, Any], raw_length: int
    ) -> None:
        request_id = _echoed_request_id(data)
        ids = peek_manual_upload_ids(data)
        if ids is None:
            # No ack the runner could match: answer with a plain error.
            await self._send_error(
                "Invalid manual_screenshot_upload: missing submission_id or key",
                request_id,
            )
            return
        submission_id, key = ids
        ok = False
        drop = False
        drop_shot = False
        message = ""
        if not self._manual_enabled or self._project is None:
            message = _MANUAL_UNSUPPORTED
        elif raw_length > MANUAL_SCREENSHOT_UPLOAD_MAX_BYTES:
            message = _MANUAL_TOO_LARGE
        else:
            try:
                # Base64, sha256 and Pillow off the shared sync thread.
                await database_sync_to_async(
                    upload_from_runner, thread_sensitive=False
                )(project=self._project, channel_name=self.channel_name, data=data)
                ok = True
            except ManualSubmissionDroppedError as exc:
                message = exc.message
                drop = True
            except ManualShotRejectedError as exc:
                # Only this shot: the hand-in's other screenshots still upload.
                message = exc.message
                drop_shot = True
            except ManualRunError as exc:
                message = exc.message
            except Exception:
                logger.exception(
                    "Manual screenshot upload failed for project %s",
                    self._project.id,
                )
                message = _MANUAL_UPLOAD_FAILED
        await self.send(
            text_data=serialize_manual_screenshot_ack(
                ManualScreenshotAckPayload(
                    submission_id=submission_id,
                    key=key,
                    ok=ok,
                    drop=drop,
                    message=message,
                    drop_shot=drop_shot,
                ),
                request_id,
            )
        )

    async def _forward_action(self, event: BaseActionEvent) -> None:
        request_id: str = event.get("request_id") or str(uuid.uuid4())
        reply_channel: str = event.get("reply_channel", "")

        if reply_channel:
            self._reply_tracker.register_reply_channel(request_id, reply_channel)

        message = self._message_builder.build_action_message(event, request_id)
        await self.send(text_data=message)

    async def controller_click(self, event: ClickActionEvent) -> None:
        await self._forward_action(event)

    async def controller_double_click(self, event: DoubleClickActionEvent) -> None:
        await self._forward_action(event)

    async def controller_hover(self, event: HoverActionEvent) -> None:
        await self._forward_action(event)

    async def controller_scroll(self, event: ScrollActionEvent) -> None:
        await self._forward_action(event)

    async def controller_drag(self, event: DragActionEvent) -> None:
        await self._forward_action(event)

    async def controller_type_text(self, event: TypeTextActionEvent) -> None:
        await self._forward_action(event)

    async def controller_key_press(self, event: KeyPressActionEvent) -> None:
        await self._forward_action(event)

    async def controller_screenshot(self, event: ScreenshotActionEvent) -> None:
        await self._forward_action(event)

    async def controller_run_command(self, event: RunCommandActionEvent) -> None:
        await self._forward_action(event)

    async def controller_browser_navigate(
        self, event: BrowserNavigateActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_browser_click(self, event: BrowserClickActionEvent) -> None:
        await self._forward_action(event)

    async def controller_browser_double_click(
        self, event: BrowserDoubleClickActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_browser_type(self, event: BrowserTypeActionEvent) -> None:
        await self._forward_action(event)

    async def controller_browser_hover(self, event: BrowserHoverActionEvent) -> None:
        await self._forward_action(event)

    async def controller_browser_get_elements(
        self, event: BrowserGetElementsActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_browser_get_page_content(
        self, event: BrowserGetPageContentActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_browser_get_url(self, event: BrowserGetUrlActionEvent) -> None:
        await self._forward_action(event)

    async def controller_browser_take_screenshot(
        self, event: BrowserTakeScreenshotActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_browser_download(
        self, event: BrowserDownloadActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_browser_list_downloads(
        self, event: BrowserListDownloadsActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_start_interactive_cmd(
        self, event: StartInteractiveCmdActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_send_input(self, event: SendInputActionEvent) -> None:
        await self._forward_action(event)

    async def controller_wait_for_command(
        self, event: WaitForCommandActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_terminate_interactive_cmd(
        self, event: TerminateInteractiveCmdActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_launch_app(self, event: LaunchAppActionEvent) -> None:
        await self._forward_action(event)

    async def controller_close_app(self, event: CloseAppActionEvent) -> None:
        await self._forward_action(event)

    async def controller_close_tab(self, event: CloseTabActionEvent) -> None:
        await self._forward_action(event)

    async def controller_check_app_installed(
        self, event: CheckAppInstalledActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_cleanup_environment(
        self, event: CleanupEnvironmentActionEvent
    ) -> None:
        await self._forward_action(event)

    async def controller_scan_directory(self, event: ScanDirectoryActionEvent) -> None:
        await self._forward_action(event)

    async def controller_inspect_paths(self, event: InspectPathsActionEvent) -> None:
        await self._forward_action(event)

    async def controller_update_runner(self, event: UpdateRunnerEvent) -> None:
        # Fire and forget: progress comes back as ``update_status`` messages.
        await self.send(
            text_data=self._message_builder.build_update_runner_message(event)
        )

    async def _send_handshake_ack(
        self,
        status: HandshakeStatus,
        message: str,
        request_id: str = "",
        project_id: str = "",
        project_name: str = "",
    ) -> None:
        ack_message = HandshakeMessageBuilder.build_handshake_ack(
            status, message, request_id, project_id, project_name
        )
        await self.send(text_data=ack_message)

    async def _send_error(self, message: str, request_id: str = "") -> None:
        error_message = HandshakeMessageBuilder.build_error(message, request_id)
        await self.send(text_data=error_message)

    @staticmethod
    def _controller_group_name(project_id: int) -> str:
        return f"controller_{project_id}"


def _echoed_request_id(data: dict[str, Any]) -> str:
    """The message's ``request_id`` when it is a short string; blank otherwise."""
    raw_request_id = data.get("request_id")
    if (
        isinstance(raw_request_id, str)
        and 0 < len(raw_request_id) <= _MAX_REQUEST_ID_LENGTH
    ):
        return raw_request_id
    return ""


def _is_manual_question(data: dict[str, Any]) -> bool:
    """The manual form of ``person_step_question``: a positive ``pivot_id``."""
    pivot_id = data.get("pivot_id")
    return isinstance(pivot_id, int) and not isinstance(pivot_id, bool) and pivot_id > 0


def _peek_submission_id(data: dict[str, Any]) -> str:
    """The message's ``submission_id`` as a normalized UUID, or blank."""
    raw = data.get("submission_id")
    if not isinstance(raw, str) or len(raw) > _MAX_SUBMISSION_ID_LENGTH:
        return ""
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        return ""


def _manual_action_refused(message: str) -> ManualCaseActionResultPayload:
    return ManualCaseActionResultPayload(ok=False, message=message)


def _manual_submit_refused(
    message: str, submission_id: str, *, drop: bool = False
) -> ManualCaseSubmitResultPayload:
    return ManualCaseSubmitResultPayload(
        ok=False, message=message, submission_id=submission_id, drop=drop
    )


def _pause_refused(message: str) -> CasePauseActionResultPayload:
    return CasePauseActionResultPayload(ok=False, outcome="", message=message)


def _case_refused(message: str) -> PersonCaseActionResultPayload:
    return PersonCaseActionResultPayload(ok=False, message=message)


def _question_refused(message: str) -> PersonStepQuestionResultPayload:
    return PersonStepQuestionResultPayload(ok=False, message=message)


def _refused(message: str) -> PersonStepActionResultPayload:
    return PersonStepActionResultPayload(ok=False, message=message, queued=False)


def _answered(
    action: PersonStepActionKind, result: PersonStepActionResult
) -> PersonStepActionResultPayload:
    if result.queued:
        return PersonStepActionResultPayload(
            ok=True, message=_PERSON_STEP_DONE[action], queued=True
        )
    return PersonStepActionResultPayload(
        ok=True,
        message=(
            f"Saved, but the run cannot continue yet: {result.blocker} "
            "Then use Continue run on the run page."
        ),
        queued=False,
    )
