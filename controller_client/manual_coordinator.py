"""The runner side of the window's Manual run tab.

Keeps the server's latest ``manual_run_state`` and the claimed case's draft
(``manual_spool``), and turns the window's requests into local work:

- a capture (Pass, Fail, Retake, case screenshot) through ``manual_capture``,
  stored in the draft with a runner-minted key;
- the person's notes, verdict and step times, kept in the draft so a window
  or runner restart loses nothing;
- a hand-in: the ``manual_case_submit`` payload is built from the draft's
  marks and the window's final texts, checked with the shared protocol
  parser (the server's own rules), committed to the spool's outbox and handed
  to the uploader. The window moves on at once; the upload runs later.

The window view it composes hides a case whose hand-in waits in the spool
(the server still shows it in progress until the hand-in arrives). The
server stays the truth for everything else: the case list, the claim and the
steps come from its snapshot. Claim and release go to the server through the
client (they need the socket).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import uuid
from dataclasses import replace

from controller_client.exceptions import ProtocolError
from controller_client.manual_capture import CaptureAbortedError, ManualCapturer
from controller_client.manual_spool import (
    ManualDraft,
    ManualSpool,
    SpoolError,
    SpoolShot,
)
from controller_client.manual_uploader import ManualUploader
from controller_client.person_window import ManualWindowRequest, PersonWindow
from controller_client.person_window_ipc import (
    ManualCaptureEvent,
    ManualCaseActionEvent,
    ManualDraftEvent,
    ManualDraftTexts,
    ManualDraftView,
    ManualMarkView,
    ManualResultKind,
    ManualSubmitEvent,
    ManualThumbnailRequestEvent,
    ManualWindowState,
)
from controller_client.protocol import (
    MANUAL_STEP_MAX_SECONDS,
    ManualActiveCasePayload,
    ManualCaseSubmitPayload,
    ManualRunStateMessage,
    ManualShotKind,
    ManualShotManifestPayload,
    ManualStepOutcome,
    ManualStepResultPayload,
    parse_manual_case_submit_payload,
    serialize_manual_case_submit,
)

logger = logging.getLogger(__name__)

# 16 URL-safe characters: within the protocol's 24-character key pattern.
_SHOT_KEY_BYTES = 12
_NOT_YOURS = "This case is no longer yours to work on."
_NO_STEP = "This step is not part of the case."
_SPOOL_FULL = (
    "The Test Runner's screenshot spool is full (CONTROLLER_MANUAL_SPOOL_MAX_MB); "
    "wait for the uploads to finish."
)
_NO_VERDICT = "Choose Passed, Failed or Blocked for the case."
_HANDED_IN = "Handed in. The screenshots upload in the background."


def _outcome_of(kind: ManualShotKind) -> ManualStepOutcome:
    return (
        ManualStepOutcome.FAIL
        if kind is ManualShotKind.FAIL
        else ManualStepOutcome.PASS
    )


def build_submit_payload(
    active: ManualActiveCasePayload, draft: ManualDraft, texts: ManualDraftTexts
) -> ManualCaseSubmitPayload:
    """The hand-in of ``draft`` with the window's final ``texts``.

    Checked with the shared parser (the server's own rules: a note for every
    failed step, a note for Blocked and for Failed without a failed step,
    note lengths, the screenshots matching their steps); raises ProtocolError
    with a message for the window.
    """
    if texts.verdict is None:
        raise ProtocolError(_NO_VERDICT)
    step_nums = {step.num for step in active.steps}
    marks = [(num, draft.marks[num]) for num in sorted(draft.marks) if num in step_nums]
    steps = tuple(
        ManualStepResultPayload(
            num=num,
            outcome=_outcome_of(shot.kind),
            note=texts.notes.get(num, "").strip(),
            shots=(shot.key,),
            seconds=min(max(texts.seconds.get(num, 0), 0), MANUAL_STEP_MAX_SECONDS),
        )
        for num, shot in marks
    )
    shots = [shot for _num, shot in marks]
    if draft.case_shot is not None:
        shots.append(draft.case_shot)
    payload = ManualCaseSubmitPayload(
        submission_id=str(uuid.uuid4()),
        pivot_id=active.pivot_id,
        claim_token=active.claim_token,
        steps=steps,
        verdict=texts.verdict,
        verdict_note=texts.verdict_note.strip(),
        shots=tuple(
            ManualShotManifestPayload(
                key=shot.key,
                step_num=shot.step_num,
                kind=shot.kind,
                sha256=shot.sha256,
                size=shot.size,
            )
            for shot in shots
        ),
    )
    # The same parser the server runs on the message.
    parse_manual_case_submit_payload(
        json.loads(serialize_manual_case_submit(payload, "check"))
    )
    return payload


def _draft_view(draft: ManualDraft) -> ManualDraftView:
    return ManualDraftView(
        pivot_id=draft.pivot_id,
        claim_token=draft.claim_token,
        marks=tuple(
            ManualMarkView(step_num=num, outcome=_outcome_of(shot.kind), key=shot.key)
            for num, shot in sorted(draft.marks.items())
        ),
        case_shot_key=draft.case_shot.key if draft.case_shot is not None else "",
        texts=draft.texts,
    )


class ManualCoordinator:
    """See the module docstring; every method runs on the runner's loop."""

    def __init__(
        self,
        *,
        window: PersonWindow,
        spool: ManualSpool,
        uploader: ManualUploader,
        capturer: ManualCapturer,
    ) -> None:
        self._window = window
        self._spool = spool
        self._uploader = uploader
        self._capturer = capturer
        self._server: ManualRunStateMessage | None = None
        # The claimed case's draft, once loaded or started.
        self._draft: ManualDraft | None = None
        # Serializes draft changes (captures, texts, hand-ins, pruning).
        self._lock = asyncio.Lock()
        # Claims handed in by this runner, recorded under the lock at commit
        # time (before the uploader's view of the spool catches up), so no
        # later capture or hand-in can reuse the claim.
        self._handed_in: set[str] = set()

    # --- server -----------------------------------------------------------------

    async def on_server_state(self, state: ManualRunStateMessage) -> None:
        """A new ``manual_run_state``: keep it, prune drafts, show it."""
        self._server = state
        self._uploader.on_server_pending(state.pending_uploads)
        server_token = state.active.claim_token if state.active is not None else None
        async with self._lock:
            # Only the server's current claim can still be handed in.
            await asyncio.to_thread(self._spool.drop_drafts_except, server_token)
            # A handed-in claim matters only while the server still shows it.
            self._handed_in &= {server_token} if server_token is not None else set()
            if self._draft is not None and self._draft.claim_token != server_token:
                self._draft = None
            active = self._visible_active()
            if active is not None and self._draft is None:
                self._draft = await asyncio.to_thread(
                    self._spool.load_draft, active.claim_token
                )
        self.push_view()

    def can_ask(self, pivot_id: int, claim_token: str) -> bool:
        """The server takes a manual question for this case now."""
        active = self._visible_active()
        return (
            active is not None
            and active.pivot_id == pivot_id
            and active.claim_token == claim_token
            and active.can_ask
        )

    def claimed(self, pivot_id: int, claim_token: str) -> bool:
        active = self._server.active if self._server is not None else None
        return (
            active is not None
            and active.pivot_id == pivot_id
            and active.claim_token == claim_token
        )

    async def on_released(self, claim_token: str) -> None:
        """The server released the claim: its draft is discarded."""
        async with self._lock:
            if self._draft is not None and self._draft.claim_token == claim_token:
                self._draft = None
            await asyncio.to_thread(self._spool.drop_draft, claim_token)
        self.push_view()

    # --- the window -----------------------------------------------------------

    def _visible_active(self) -> ManualActiveCasePayload | None:
        """The claimed case unless its hand-in already waits in the spool."""
        server = self._server
        if server is None or server.active is None:
            return None
        handed_in = {sub.claim_token for sub in self._uploader.submissions}
        token = server.active.claim_token
        if token in handed_in or token in self._handed_in:
            return None
        return server.active

    def push_view(self) -> None:
        server = self._server
        if server is None:
            return
        active = self._visible_active()
        draft = self._draft
        draft_view = (
            _draft_view(draft)
            if active is not None
            and draft is not None
            and draft.claim_token == active.claim_token
            else None
        )
        submitted = tuple(
            sorted(
                {sub.pivot_id for sub in self._uploader.submissions if not sub.accepted}
            )
        )
        self._window.apply_manual_state(
            ManualWindowState(
                run=server.run,
                cases=server.cases,
                active=active,
                draft=draft_view,
                submitted_pivots=submitted,
                upload=self._uploader.status(),
                spool_full=self._uploader.spool_full,
            )
        )

    async def handle(self, request: ManualWindowRequest) -> None:
        """One window request (claim and release are the client's)."""
        match request:
            case ManualCaptureEvent():
                await self._capture(request)
            case ManualDraftEvent():
                await self._save_texts(request)
            case ManualSubmitEvent():
                await self._submit(request)
            case ManualThumbnailRequestEvent(key=key):
                await self._send_thumbnail(key)
            case ManualCaseActionEvent():
                logger.error("A manual case action reached the coordinator")

    def _active_for(
        self, pivot_id: int, claim_token: str
    ) -> ManualActiveCasePayload | None:
        active = self._visible_active()
        if (
            active is None
            or active.pivot_id != pivot_id
            or active.claim_token != claim_token
        ):
            return None
        return active

    def _draft_for(self, active: ManualActiveCasePayload) -> ManualDraft:
        draft = self._draft
        if draft is not None and draft.claim_token == active.claim_token:
            return draft
        return ManualDraft(pivot_id=active.pivot_id, claim_token=active.claim_token)

    async def _capture(self, event: ManualCaptureEvent) -> None:
        active = self._active_for(event.pivot_id, event.claim_token)
        if active is None:
            self._window.show_capture_result(event.seq, False, _NOT_YOURS)
            return
        if event.step_num is not None and event.step_num not in {
            step.num for step in active.steps
        }:
            self._window.show_capture_result(event.seq, False, _NO_STEP)
            return
        if self._uploader.spool_full:
            self._window.show_capture_result(event.seq, False, _SPOOL_FULL)
            return
        try:
            shot = await self._capturer.capture()
        except CaptureAbortedError as e:
            self._window.show_capture_result(event.seq, False, str(e))
            return
        key = secrets.token_urlsafe(_SHOT_KEY_BYTES)
        async with self._lock:
            # The claim may have changed while the screen was captured.
            active = self._active_for(event.pivot_id, event.claim_token)
            if active is None:
                self._window.show_capture_result(event.seq, False, _NOT_YOURS)
                return
            spool_shot = SpoolShot(
                key=key,
                step_num=event.step_num,
                kind=event.kind,
                mime=shot.mime,
                sha256=shot.sha256,
                size=len(shot.data),
            )
            try:
                self._draft = await asyncio.to_thread(
                    self._spool.add_shot,
                    self._draft_for(active),
                    spool_shot,
                    shot.data,
                    shot.thumbnail_png,
                )
            except (SpoolError, OSError) as e:
                logger.warning("Could not store a manual screenshot: %s", e)
                message = (
                    str(e)
                    if isinstance(e, SpoolError)
                    else "The screenshot could not be saved on this machine."
                )
                self._window.show_capture_result(event.seq, False, message)
                await self._uploader.refresh()
                return
        self._window.show_capture_result(
            event.seq,
            True,
            "",
            key=key,
            thumbnail_b64=base64.b64encode(shot.thumbnail_png).decode("ascii"),
        )
        await self._uploader.refresh()

    async def _save_texts(self, event: ManualDraftEvent) -> None:
        async with self._lock:
            active = self._active_for(event.pivot_id, event.claim_token)
            if active is None:
                return
            draft = replace(self._draft_for(active), texts=event.texts)
            try:
                await asyncio.to_thread(self._spool.save_draft, draft)
            except (SpoolError, OSError) as e:
                logger.warning("Could not save the manual case draft: %s", e)
            self._draft = draft

    async def _submit(self, event: ManualSubmitEvent) -> None:
        async with self._lock:
            active = self._active_for(event.pivot_id, event.claim_token)
            if active is None:
                self._window.show_manual_result(
                    ManualResultKind.SUBMIT, False, _NOT_YOURS
                )
                return
            draft = replace(self._draft_for(active), texts=event.texts)
            self._draft = draft
            try:
                payload = build_submit_payload(active, draft, event.texts)
            except ProtocolError as e:
                self._window.show_manual_result(ManualResultKind.SUBMIT, False, str(e))
                return
            try:
                await asyncio.to_thread(self._spool.save_draft, draft)
                submission = await asyncio.to_thread(
                    self._spool.commit, draft, payload, active.title
                )
            except (SpoolError, OSError) as e:
                logger.error("Could not store the manual hand-in: %s", e)
                message = (
                    str(e)
                    if isinstance(e, SpoolError)
                    else "The result could not be saved on this machine."
                )
                self._window.show_manual_result(ManualResultKind.SUBMIT, False, message)
                return
            self._draft = None
            self._handed_in.add(active.claim_token)
        logger.info(
            "Handed in run case %d (%s, %d screenshot(s)) as %s",
            submission.pivot_id,
            payload.verdict.value,
            len(payload.shots),
            submission.submission_id,
        )
        await self._uploader.refresh()
        self._uploader.wake(new_hand_in=True)
        self._window.show_manual_result(ManualResultKind.SUBMIT, True, _HANDED_IN)
        self.push_view()

    async def _send_thumbnail(self, key: str) -> None:
        draft = self._draft
        data = (
            await asyncio.to_thread(self._spool.read_thumbnail, draft.claim_token, key)
            if draft is not None and draft.shot(key) is not None
            else None
        )
        self._window.send_thumbnail(
            key, base64.b64encode(data).decode("ascii") if data else ""
        )
