"""Background sending of Manual run hand-ins and their screenshots.

One asyncio task (``run``) works through the spool (``manual_spool``) while
the runner has an authenticated session:

1. Every hand-in the server has not accepted yet is sent first
   (``manual_case_submit``; the server is idempotent on ``submission_id``, so
   a hand-in whose result was lost is simply sent again). Accepted: the
   ``accepted`` marker is written and the server's ``missing`` keys are kept;
   ``drop``: the hand-in is deleted (the claim was released, the case re-run,
   ...); any other refusal is retried with a backoff.
2. Then the screenshots, one at a time (``manual_screenshot_upload``): a
   screenshot is deleted once acknowledged; an ack with ``drop`` deletes the
   whole hand-in, one with ``drop_shot`` only that screenshot (with a notice);
   any other refusal is retried with a backoff. Screenshots the server
   says it already has (``missing`` of a submit result or of the snapshot's
   ``pending_uploads``) are deleted without uploading.

A disconnect stops the work (the waiting reply is failed) and the next
session resumes it, as does a restart: everything lives on disk. Results are
matched to their message by ``request_id``; only one message is in flight.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final

from controller_client.manual_spool import ManualSpool, SpooledSubmission, SpoolError
from controller_client.person_window_ipc import ManualUploadView
from controller_client.protocol import (
    ManualCaseSubmitResultPayload,
    ManualPendingUploadPayload,
    ManualScreenshotAckPayload,
    ManualScreenshotUploadPayload,
    serialize_manual_case_submit,
    serialize_manual_screenshot_upload,
)

logger = logging.getLogger(__name__)

SUBMIT_REPLY_TIMEOUT_SECONDS: Final[float] = 30.0
# After the message was written; a 9 MiB message on a slow uplink takes time.
UPLOAD_REPLY_TIMEOUT_SECONDS: Final[float] = 120.0
_BACKOFF_START_SECONDS: Final[float] = 2.0
_BACKOFF_MAX_SECONDS: Final[float] = 60.0
_TITLE_IN_MESSAGE_MAX_CHARS: Final[int] = 80
# Refusals of one hand-in or screenshot before it is skipped for the rest of
# the session, so one item the server keeps refusing never holds up the rest.
_MAX_FAILURES_PER_SESSION: Final[int] = 5

Sender = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class _Work:
    submission: SpooledSubmission
    # None: send the hand-in itself; else upload this screenshot.
    key: str | None


def _short_title(title: str) -> str:
    title = title.strip() or "a case"
    if len(title) > _TITLE_IN_MESSAGE_MAX_CHARS:
        return title[: _TITLE_IN_MESSAGE_MAX_CHARS - 1] + "…"
    return title


class ManualUploader:
    """Sends the spool's hand-ins and screenshots (see the module docstring)."""

    def __init__(self, spool: ManualSpool, on_change: Callable[[], None]) -> None:
        self._spool = spool
        self._on_change = on_change
        self._send: Sender | None = None
        # Set on attach / detach so a backoff ends with the session.
        self._session_changed = asyncio.Event()
        self._wake = asyncio.Event()
        # The reply awaited: (request_id, future). The future gets the parsed
        # reply, or None when the session ended or the reply was unreadable.
        self._waiting: tuple[str, asyncio.Future[object | None]] | None = None
        # Per hand-in: the screenshot keys the server last said it lacks.
        self._missing: dict[str, frozenset[str]] = {}
        # Failures this session per (submission_id, key or None for the hand-in).
        self._failures: dict[tuple[str, str | None], int] = {}
        self._submissions: tuple[SpooledSubmission, ...] = ()
        self._spool_full = False
        self._sending = False
        # A refusal being retried (cleared by the next success) and a hand-in
        # the server will never take (kept until the next hand-in).
        self._error = ""
        self._notice = ""

    # --- state for the window -------------------------------------------------

    @property
    def submissions(self) -> tuple[SpooledSubmission, ...]:
        """The hand-ins in the spool at the last refresh, oldest first."""
        return self._submissions

    @property
    def spool_full(self) -> bool:
        return self._spool_full

    def status(self) -> ManualUploadView:
        return ManualUploadView(
            waiting_cases=len(self._submissions),
            waiting_shots=sum(len(sub.remaining) for sub in self._submissions),
            unsent_cases=sum(1 for sub in self._submissions if not sub.accepted),
            sending=self._sending,
            message=self._error or self._notice,
        )

    async def refresh(self) -> None:
        """Re-read the spool (hand-ins and size) and tell the window."""
        self._submissions = await asyncio.to_thread(self._spool.submissions)
        self._spool_full = await asyncio.to_thread(self._spool.is_full)
        known = {sub.submission_id for sub in self._submissions}
        self._missing = {
            sid: keys for sid, keys in self._missing.items() if sid in known
        }
        self._on_change()

    # --- session --------------------------------------------------------------

    def attach(self, send: Sender) -> None:
        """An authenticated session started: work through the spool."""
        self._send = send
        self._failures.clear()
        self._session_changed.set()
        self.wake()

    def detach(self) -> None:
        """The session ended: fail the waiting reply and stop working."""
        self._send = None
        self._session_changed.set()
        self._settle(None)

    def wake(self, *, new_hand_in: bool = False) -> None:
        """Something changed in the spool (``new_hand_in``: clears the notice
        about an earlier hand-in the server refused)."""
        if new_hand_in:
            self._notice = ""
        self._wake.set()

    def on_server_pending(self, pending: Sequence[ManualPendingUploadPayload]) -> None:
        """The snapshot's hand-ins still uploading, with their missing keys."""
        changed = False
        for upload in pending:
            keys = frozenset(upload.missing)
            if self._missing.get(upload.submission_id) != keys:
                self._missing[upload.submission_id] = keys
                changed = True
        if changed:
            self.wake()

    def on_submit_result(
        self, request_id: str, result: ManualCaseSubmitResultPayload | None
    ) -> bool:
        """A ``manual_case_submit_result``; False when no hand-in waits for it."""
        return self._settle_if(request_id, result)

    def on_ack(self, request_id: str, ack: ManualScreenshotAckPayload | None) -> bool:
        """A ``manual_screenshot_ack``; False when no upload waits for it."""
        return self._settle_if(request_id, ack)

    def _settle_if(self, request_id: str, reply: object | None) -> bool:
        waiting = self._waiting
        if waiting is None or waiting[0] != request_id:
            return False
        self._settle(reply)
        return True

    def _settle(self, reply: object | None) -> None:
        waiting = self._waiting
        if waiting is None:
            return
        self._waiting = None
        if not waiting[1].done():
            waiting[1].set_result(reply)

    # --- the work loop --------------------------------------------------------

    async def run(self) -> None:
        """Forever: refresh on every wake and drain the spool while connected."""
        backoff = _BACKOFF_START_SECONDS
        self._wake.set()
        while True:
            await self._wake.wait()
            self._wake.clear()
            try:
                await self.refresh()
                while self._send is not None:
                    work = await self._next_work()
                    if work is None:
                        break
                    if await self._do(work):
                        backoff = _BACKOFF_START_SECONDS
                        continue
                    if self._send is None:
                        break
                    item = (work.submission.submission_id, work.key)
                    self._failures[item] = self._failures.get(item, 0) + 1
                    await self._sleep_unless_session_changes(backoff)
                    backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Manual uploads failed; retrying later")
                self._error = "Uploading screenshots failed; retrying."
                self._sending = False
                self._on_change()
                await self._sleep_unless_session_changes(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)
                self._wake.set()

    async def _sleep_unless_session_changes(self, seconds: float) -> None:
        self._session_changed.clear()
        try:
            await asyncio.wait_for(self._session_changed.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _next_work(self) -> _Work | None:
        await self.refresh()
        for submission in self._submissions:
            if not submission.accepted and not self._skipped(submission, None):
                return _Work(submission, None)
        for submission in self._submissions:
            if not submission.accepted:
                continue
            missing = self._missing.get(submission.submission_id)
            remaining = submission.remaining
            if missing is not None:
                done = [key for key in remaining if key not in missing]
                if done:
                    # The server already has these (an earlier session's
                    # upload whose ack was lost).
                    await asyncio.to_thread(
                        self._spool.delete_shots, submission.submission_id, done
                    )
                    return await self._next_work()
            for key in remaining:
                if not self._skipped(submission, key):
                    return _Work(submission, key)
        return None

    def _skipped(self, submission: SpooledSubmission, key: str | None) -> bool:
        failures = self._failures.get((submission.submission_id, key), 0)
        return failures >= _MAX_FAILURES_PER_SESSION

    async def _do(self, work: _Work) -> bool:
        self._sending = True
        self._on_change()
        try:
            if work.key is None:
                return await self._send_hand_in(work.submission)
            return await self._upload(work.submission, work.key)
        finally:
            self._sending = False
            self._on_change()

    async def _request(
        self, request_id: str, message: str, timeout: float
    ) -> object | None:
        """Send one message and wait for its reply (None: none came)."""
        send = self._send
        if send is None:
            return None
        future: asyncio.Future[object | None] = (
            asyncio.get_running_loop().create_future()
        )
        self._waiting = (request_id, future)
        try:
            await send(message)
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            logger.warning("No reply from the server to a manual upload message")
            return None
        except Exception as e:  # the session ended while sending
            logger.info("Manual upload message not sent: %s", e)
            return None
        finally:
            if self._waiting is not None and self._waiting[0] == request_id:
                self._waiting = None

    async def _send_hand_in(self, submission: SpooledSubmission) -> bool:
        title = _short_title(submission.title)
        logger.info(
            "Sending the manual hand-in %s for run case %d",
            submission.submission_id,
            submission.pivot_id,
        )
        request_id = str(uuid.uuid4())
        message = serialize_manual_case_submit(submission.payload, request_id)
        reply = await self._request(request_id, message, SUBMIT_REPLY_TIMEOUT_SECONDS)
        if not isinstance(reply, ManualCaseSubmitResultPayload):
            return False
        if reply.ok:
            missing = frozenset(reply.missing)
            self._missing[submission.submission_id] = missing
            await asyncio.to_thread(self._spool.mark_accepted, submission.submission_id)
            done = [key for key in submission.remaining if key not in missing]
            if done:
                await asyncio.to_thread(
                    self._spool.delete_shots, submission.submission_id, done
                )
            self._error = ""
            return True
        if reply.drop:
            logger.warning(
                "The server will not take the hand-in for run case %d: %s",
                submission.pivot_id,
                reply.message,
            )
            await asyncio.to_thread(
                self._spool.drop_submission, submission.submission_id
            )
            self._notice = (
                f"Punk Hazard did not take your result for “{title}”: "
                f"{reply.message or 'the case changed meanwhile.'}"
            )
            self._error = ""
            return True
        self._error = (
            f"Sending your result for “{title}” failed: "
            f"{reply.message or 'refused'}. Retrying."
        )
        return False

    async def _upload(self, submission: SpooledSubmission, key: str) -> bool:
        sid = submission.submission_id
        try:
            stored = await asyncio.to_thread(self._spool.read_shot, sid, key)
        except SpoolError:
            stored = None
        manifest = next((s for s in submission.payload.shots if s.key == key), None)
        if stored is None or manifest is None:
            # Gone meanwhile: the next refresh no longer lists it.
            return True
        data, mime = stored
        if hashlib.sha256(data).hexdigest() != manifest.sha256:
            logger.error(
                "Manual screenshot %s of hand-in %s changed on disk; it cannot be "
                "uploaded and is deleted",
                key,
                sid,
            )
            await asyncio.to_thread(self._spool.delete_shots, sid, [key])
            return True
        upload = ManualScreenshotUploadPayload(
            submission_id=sid, key=key, sha256=manifest.sha256, mime=mime, data=data
        )
        request_id = str(uuid.uuid4())
        message = await asyncio.to_thread(
            serialize_manual_screenshot_upload, upload, request_id
        )
        reply = await self._request(request_id, message, UPLOAD_REPLY_TIMEOUT_SECONDS)
        if not isinstance(reply, ManualScreenshotAckPayload):
            return False
        if reply.ok:
            await asyncio.to_thread(self._spool.delete_shots, sid, [key])
            self._error = ""
            return True
        if reply.drop:
            logger.warning(
                "The server will not take screenshots of hand-in %s: %s",
                sid,
                reply.message,
            )
            await asyncio.to_thread(self._spool.drop_submission, sid)
            self._error = ""
            return True
        if reply.drop_shot:
            # Only this screenshot is refused for good; the others still go.
            logger.warning(
                "The server refused screenshot %s of hand-in %s: %s",
                key,
                sid,
                reply.message,
            )
            await asyncio.to_thread(self._spool.delete_shots, sid, [key])
            self._notice = (
                f"Punk Hazard refused a screenshot of "
                f"“{_short_title(submission.title)}”: "
                f"{reply.message or 'it could not be stored.'}"
            )
            self._error = ""
            return True
        self._error = (
            f"Uploading a screenshot of “{_short_title(submission.title)}” "
            f"failed: {reply.message or 'refused'}. Retrying."
        )
        return False
