"""The Test Runner's local spool of manual run screenshots and hand-ins.

Screenshots of a Manual run are taken on the test machine and uploaded only
after the tester hands the case in, so the tester never waits for the
network. Everything lives under ``controller_client/manual_spool/`` (git-
and docker-ignored) and survives a runner restart:

- ``drafts/<claim_token>/``: the claimed case being worked on:
  ``draft.json`` (the marked steps with their screenshots, the optional case
  screenshot, the person's texts and the screenshots of an open mark sheet,
  "staged" until it is saved) and the screenshots taken so far, ``<key>.png``
  or ``<key>.jpg``, each with a ``<key>.thumb.png``. Staged screenshots count
  toward the size cap and are dropped when a draft is loaded (the sheet that
  took them is gone) and at hand-in.
- ``outbox/<submission_id>/``: a hand-in: ``manifest.json`` (the
  ``manual_case_submit`` payload and each screenshot's image type) and the
  screenshots still to upload. An empty ``accepted`` file marks a hand-in the
  server took.

A hand-in is made by writing ``manifest.json`` into the draft folder and then
renaming that folder into ``outbox/`` (atomic on one file system); a draft
folder that already holds a manifest (a crash in between) is moved by
``recover()``. A screenshot is deleted once the server acknowledged it, and a
hand-in folder once it was accepted and holds no screenshot any more.

All methods do blocking file I/O: call them from a worker thread. They are
serialized by one lock, so the uploader and the window's requests never see
a half-made folder. Folder and file names come from the runner (claim tokens
and keys are checked against the protocol's patterns), never from the disk
alone: anything else found in the spool is ignored.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from controller_client.exceptions import ProtocolError
from controller_client.person_window_ipc import (
    ManualDraftTexts,
    manual_texts_from_wire,
    manual_texts_to_wire,
)
from controller_client.protocol import (
    MANUAL_CASE_MAX_STEPS,
    MANUAL_CLAIM_TOKEN_MAX_CHARS,
    MANUAL_SHOT_KEY_MAX_CHARS,
    MANUAL_SHOT_MAX_BYTES,
    MANUAL_STEP_MAX_SHOTS,
    ManualCaseSubmitPayload,
    ManualShotKind,
    ManualShotMime,
    parse_manual_case_submit_payload,
    serialize_manual_case_submit,
)

logger = logging.getLogger(__name__)

SPOOL_DIR: Final[Path] = Path(__file__).resolve().parent / "manual_spool"
# CONTROLLER_MANUAL_SPOOL_MAX_MB default and floor (a floor of a few
# full-size screenshots, so a tiny value cannot make the window unusable).
DEFAULT_SPOOL_MAX_MB: Final[int] = 2048
MIN_SPOOL_MAX_MB: Final[int] = 64

_DRAFTS: Final[str] = "drafts"
_OUTBOX: Final[str] = "outbox"
_DRAFT_FILE: Final[str] = "draft.json"
_MANIFEST_FILE: Final[str] = "manifest.json"
_ACCEPTED_FILE: Final[str] = "accepted"
_THUMB_SUFFIX: Final[str] = ".thumb.png"
# Draft version 2 (Test Runner 0.17.0): several screenshots per mark and the
# staged ones; version 1 drafts (one screenshot per mark) are still read.
_FORMAT_VERSION: Final[int] = 2
_FORMAT_VERSION_ONE_SHOT: Final[int] = 1
# Hand-in manifests did not change.
_MANIFEST_VERSION: Final[int] = 1
# Largest draft.json / manifest.json read back (both are far smaller).
_JSON_MAX_BYTES: Final[int] = 4 * 1024 * 1024
_TITLE_MAX_CHARS: Final[int] = 500
_EXTENSIONS: Final[dict[ManualShotMime, str]] = {
    ManualShotMime.PNG: ".png",
    ManualShotMime.JPEG: ".jpg",
}
_CLAIM_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    rf"[A-Za-z0-9_-]{{1,{MANUAL_CLAIM_TOKEN_MAX_CHARS}}}"
)
_SHOT_KEY_RE: Final[re.Pattern[str]] = re.compile(
    rf"[A-Za-z0-9_-]{{1,{MANUAL_SHOT_KEY_MAX_CHARS}}}"
)
_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")


class SpoolError(Exception):
    """A spool operation failed; the message is safe to show in the window."""


class SpoolFullError(SpoolError):
    """The spool is at CONTROLLER_MANUAL_SPOOL_MAX_MB."""


@dataclass(frozen=True)
class SpoolShot:
    """One screenshot of a draft, as its hand-in will announce it."""

    key: str
    # None for the case screenshot.
    step_num: int | None
    kind: ManualShotKind
    mime: ManualShotMime
    # Lowercase hex of the image bytes.
    sha256: str
    size: int

    @property
    def file_name(self) -> str:
        return f"{self.key}{_EXTENSIONS[self.mime]}"

    def to_json(self) -> dict[str, object]:
        return {
            "key": self.key,
            "step_num": self.step_num,
            "kind": self.kind.value,
            "mime": self.mime.value,
            "sha256": self.sha256,
            "size": self.size,
        }


@dataclass(frozen=True)
class ManualDraft:
    """The claimed case's marks (the runner's) and texts (the window's)."""

    pivot_id: int
    claim_token: str
    # Step number -> its screenshots in the order taken (1 or more, all of
    # the same kind: the step's outcome).
    marks: Mapping[int, tuple[SpoolShot, ...]] = field(default_factory=dict)
    case_shot: SpoolShot | None = None
    texts: ManualDraftTexts = field(default_factory=ManualDraftTexts)
    # Step screenshots of an open mark sheet, not part of any mark yet.
    staged: tuple[SpoolShot, ...] = ()

    def shots(self) -> tuple[SpoolShot, ...]:
        """Every screenshot a hand-in may announce: the marks' in step order,
        then the case one (never the staged ones)."""
        ordered = tuple(shot for num in sorted(self.marks) for shot in self.marks[num])
        return ordered + ((self.case_shot,) if self.case_shot is not None else ())

    def shot(self, key: str) -> SpoolShot | None:
        """A screenshot of the draft, staged or not."""
        return next(
            (shot for shot in self.shots() + self.staged if shot.key == key), None
        )

    def staged_for(self, step_num: int) -> tuple[SpoolShot, ...]:
        return tuple(shot for shot in self.staged if shot.step_num == step_num)


@dataclass(frozen=True)
class SpooledSubmission:
    """A hand-in waiting in the outbox."""

    submission_id: str
    pivot_id: int
    claim_token: str
    # The case title at hand-in time, for messages in the window.
    title: str
    payload: ManualCaseSubmitPayload
    mimes: Mapping[str, ManualShotMime]
    # The server took the hand-in (only its screenshots remain).
    accepted: bool
    # Screenshot keys whose files are still in the spool, in manifest order.
    remaining: tuple[str, ...]


def _is_claim_token(value: str) -> bool:
    return bool(_CLAIM_TOKEN_RE.fullmatch(value))


def _is_shot_key(value: str) -> bool:
    return bool(_SHOT_KEY_RE.fullmatch(value))


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _write_json(path: Path, data: Mapping[str, object]) -> None:
    _write_atomic(
        path, json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    )


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        if path.stat().st_size > _JSON_MAX_BYTES:
            return None
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _shot_from_json(raw: object) -> SpoolShot:
    if not isinstance(raw, dict):
        raise ValueError("not an object")
    key = raw.get("key")
    step_num = raw.get("step_num")
    sha256 = raw.get("sha256")
    size = raw.get("size")
    if not isinstance(key, str) or not _is_shot_key(key):
        raise ValueError("bad key")
    if step_num is not None and (
        not isinstance(step_num, int)
        or isinstance(step_num, bool)
        or not 1 <= step_num <= MANUAL_CASE_MAX_STEPS
    ):
        raise ValueError("bad step")
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise ValueError("bad sha256")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or not 1 <= size <= MANUAL_SHOT_MAX_BYTES
    ):
        raise ValueError("bad size")
    return SpoolShot(
        key=key,
        step_num=step_num,
        kind=ManualShotKind(str(raw.get("kind"))),
        mime=ManualShotMime(str(raw.get("mime"))),
        sha256=sha256,
        size=size,
    )


def _step_shot_from_json(raw: object, step_num: int | None = None) -> SpoolShot:
    """A step screenshot (of ``step_num`` when given)."""
    shot = _shot_from_json(raw)
    if (
        shot.step_num is None
        or shot.kind is ManualShotKind.CASE
        or (step_num is not None and shot.step_num != step_num)
    ):
        raise ValueError("bad step screenshot")
    return shot


def _mark_from_json(raw_num: str, raw: object, version: int) -> tuple[SpoolShot, ...]:
    if not raw_num.isdigit():
        raise ValueError("bad mark")
    num = int(raw_num)
    if version == _FORMAT_VERSION_ONE_SHOT:
        return (_step_shot_from_json(raw, num),)
    if not isinstance(raw, list) or not 1 <= len(raw) <= MANUAL_STEP_MAX_SHOTS:
        raise ValueError("bad mark")
    shots = tuple(_step_shot_from_json(item, num) for item in raw)
    if len({shot.kind for shot in shots}) != 1:
        raise ValueError("bad mark")
    return shots


def _draft_to_json(draft: ManualDraft) -> dict[str, object]:
    return {
        "version": _FORMAT_VERSION,
        "pivot_id": draft.pivot_id,
        "claim_token": draft.claim_token,
        "marks": {
            str(num): [shot.to_json() for shot in shots]
            for num, shots in draft.marks.items()
        },
        "case_shot": draft.case_shot.to_json() if draft.case_shot else None,
        "texts": manual_texts_to_wire(draft.texts),
        "staged": [shot.to_json() for shot in draft.staged],
    }


def _draft_from_json(data: Mapping[str, object], claim_token: str) -> ManualDraft:
    version = data.get("version")
    pivot_id = data.get("pivot_id")
    raw_marks = data.get("marks")
    raw_texts = data.get("texts")
    raw_staged = data.get("staged", [])
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version not in (_FORMAT_VERSION, _FORMAT_VERSION_ONE_SHOT)
        or data.get("claim_token") != claim_token
        or not isinstance(pivot_id, int)
        or isinstance(pivot_id, bool)
        or pivot_id < 1
        or not isinstance(raw_marks, dict)
        or not isinstance(raw_texts, dict)
        or not isinstance(raw_staged, list)
    ):
        raise ValueError("bad draft")
    marks = {
        int(raw_num): _mark_from_json(str(raw_num), raw_shots, version)
        for raw_num, raw_shots in raw_marks.items()
    }
    raw_case = data.get("case_shot")
    case_shot = _shot_from_json(raw_case) if raw_case is not None else None
    if case_shot is not None and (
        case_shot.step_num is not None or case_shot.kind is not ManualShotKind.CASE
    ):
        raise ValueError("bad case screenshot")
    staged = tuple(_step_shot_from_json(raw) for raw in raw_staged)
    keys = [
        shot.key
        for shot in (
            *(shot for shots in marks.values() for shot in shots),
            *staged,
            *((case_shot,) if case_shot is not None else ()),
        )
    ]
    if len(set(keys)) != len(keys):
        raise ValueError("repeated screenshot key")
    try:
        texts = manual_texts_from_wire(raw_texts)
    except ProtocolError as e:
        raise ValueError(str(e)) from e
    return ManualDraft(
        pivot_id=pivot_id,
        claim_token=claim_token,
        marks=marks,
        case_shot=case_shot,
        texts=texts,
        staged=staged,
    )


class ManualSpool:
    """The on-disk spool (see the module docstring)."""

    def __init__(self, root: Path = SPOOL_DIR, *, max_bytes: int) -> None:
        self._root = root
        self._max_bytes = max_bytes
        self._lock = threading.Lock()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    def _drafts(self) -> Path:
        return self._root / _DRAFTS

    def _outbox(self) -> Path:
        return self._root / _OUTBOX

    def _draft_dir(self, claim_token: str) -> Path:
        if not _is_claim_token(claim_token):
            raise SpoolError("Invalid claim.")
        return self._drafts() / claim_token

    def _submission_dir(self, submission_id: str) -> Path:
        if not _UUID_RE.fullmatch(submission_id):
            raise SpoolError("Invalid hand-in id.")
        return self._outbox() / submission_id

    # --- size -------------------------------------------------------------

    def _used_bytes(self) -> int:
        total = 0
        if not self._root.is_dir():
            return 0
        for path in self._root.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
        return total

    def is_full(self) -> bool:
        """At or over the cap: the window takes no new screenshots."""
        with self._lock:
            return self._used_bytes() >= self._max_bytes

    def is_empty(self) -> bool:
        """No hand-in and no draft screenshot (empty folders do not count)."""
        with self._lock:
            if not self._root.is_dir():
                return True
            return not any(path.is_file() for path in self._root.rglob("*"))

    def describe(self) -> str:
        """ "3 hand-ins, 1 draft" for log lines and refusals."""
        with self._lock:
            hand_ins = len(self._submission_dirs())
            drafts = sum(
                1
                for path in self._draft_dirs()
                if any(child.is_file() for child in path.iterdir())
            )
        return f"{hand_ins} hand-in(s) and {drafts} case draft(s) in {self._root}"

    # --- recovery -----------------------------------------------------------

    def recover(self) -> None:
        """Finish hand-ins a crash left half-made (call once at start)."""
        with self._lock:
            for path in self._draft_dirs():
                manifest = _read_json(path / _MANIFEST_FILE)
                if manifest is None:
                    continue
                submission_id = manifest.get("submission_id")
                if not isinstance(submission_id, str) or not _UUID_RE.fullmatch(
                    submission_id
                ):
                    continue
                target = self._outbox() / submission_id
                if target.exists():
                    continue
                self._outbox().mkdir(parents=True, exist_ok=True)
                os.replace(path, target)
                self._clean_submission_dir(target, manifest)
                logger.info("Recovered manual hand-in %s from its draft", submission_id)

    # --- drafts ---------------------------------------------------------------

    def _draft_dirs(self) -> list[Path]:
        drafts = self._drafts()
        if not drafts.is_dir():
            return []
        return [
            path
            for path in drafts.iterdir()
            if path.is_dir() and not path.is_symlink() and _is_claim_token(path.name)
        ]

    def load_draft(self, claim_token: str) -> ManualDraft | None:
        """The stored draft of a claim, or None when there is none (or it is
        unreadable: its screenshots are then retaken). Its staged screenshots
        are dropped: the mark sheet that took them is gone."""
        with self._lock:
            path = self._draft_dir(claim_token) / _DRAFT_FILE
            data = _read_json(path)
            if data is None:
                return None
            try:
                draft = _draft_from_json(data, claim_token)
            except (ValueError, KeyError) as e:
                logger.warning("Ignoring an unreadable manual draft: %s", e)
                return None
            folder = path.parent
            # Screenshots whose file is gone (deleted by hand) are dropped,
            # and a mark left without any with them.
            marks: dict[int, tuple[SpoolShot, ...]] = {}
            for num, shots in draft.marks.items():
                kept = tuple(
                    shot for shot in shots if (folder / shot.file_name).is_file()
                )
                if kept:
                    marks[num] = kept
            case_shot = draft.case_shot
            if case_shot is not None and not (folder / case_shot.file_name).is_file():
                case_shot = None
            draft = replace(draft, marks=marks, case_shot=case_shot)
            try:
                return self._drop_staged(folder, draft)
            except OSError as e:
                logger.warning("Could not drop staged manual screenshots: %s", e)
                return replace(draft, staged=())

    def save_draft(self, draft: ManualDraft) -> None:
        with self._lock:
            folder = self._draft_dir(draft.claim_token)
            folder.mkdir(parents=True, exist_ok=True)
            _write_json(folder / _DRAFT_FILE, _draft_to_json(draft))

    def add_shot(
        self,
        draft: ManualDraft,
        shot: SpoolShot,
        data: bytes,
        thumbnail: bytes,
        *,
        staged: bool = False,
    ) -> ManualDraft:
        """Store a screenshot and put it in the draft; returns the saved
        draft. SpoolFullError at the cap.

        A staged step screenshot only joins ``staged`` (``commit_mark`` makes
        it part of the mark). Otherwise it replaces the case screenshot, or
        the step's whole mark with this one screenshot.
        """
        if staged and shot.step_num is None:
            raise SpoolError("Only a step screenshot can wait in a mark sheet.")
        with self._lock:
            if self._used_bytes() + len(data) + len(thumbnail) > self._max_bytes:
                raise SpoolFullError(
                    "The Test Runner's screenshot spool is full "
                    "(CONTROLLER_MANUAL_SPOOL_MAX_MB); wait for the uploads to "
                    "finish."
                )
            folder = self._draft_dir(draft.claim_token)
            folder.mkdir(parents=True, exist_ok=True)
            _write_atomic(folder / shot.file_name, data)
            _write_atomic(folder / f"{shot.key}{_THUMB_SUFFIX}", thumbnail)
            replaced: tuple[SpoolShot, ...]
            if staged:
                replaced = ()
                updated = replace(draft, staged=(*draft.staged, shot))
            elif shot.step_num is None:
                replaced = (draft.case_shot,) if draft.case_shot is not None else ()
                updated = replace(draft, case_shot=shot)
            else:
                replaced = draft.marks.get(shot.step_num, ())
                marks = dict(draft.marks)
                marks[shot.step_num] = (shot,)
                updated = replace(draft, marks=marks)
            _write_json(folder / _DRAFT_FILE, _draft_to_json(updated))
            for old in replaced:
                self._delete_draft_shot(folder, old)
            return updated

    def commit_mark(
        self,
        draft: ManualDraft,
        step_num: int,
        kind: ManualShotKind,
        keys: tuple[str, ...],
    ) -> ManualDraft:
        """Save a mark sheet: ``keys`` (in order) become the step's mark of
        ``kind``; returns the saved draft.

        Each key is staged for the step or already part of its mark (a sheet
        reopened to edit the mark). The mark's other screenshots and the
        step's other staged ones are deleted. SpoolError when a key is neither.
        """
        if kind is ManualShotKind.CASE or not keys or len(set(keys)) != len(keys):
            raise SpoolError("Invalid mark.")
        with self._lock:
            folder = self._draft_dir(draft.claim_token)
            available = {
                shot.key: shot
                for shot in (
                    *draft.marks.get(step_num, ()),
                    *draft.staged_for(step_num),
                )
            }
            if any(key not in available for key in keys):
                raise SpoolError(
                    "A screenshot of this step is no longer on this machine; "
                    "take it again."
                )
            chosen = tuple(replace(available[key], kind=kind) for key in keys)
            marks = dict(draft.marks)
            marks[step_num] = chosen
            updated = replace(
                draft,
                marks=marks,
                staged=tuple(
                    shot for shot in draft.staged if shot.step_num != step_num
                ),
            )
            folder.mkdir(parents=True, exist_ok=True)
            _write_json(folder / _DRAFT_FILE, _draft_to_json(updated))
            for key, shot in available.items():
                if key not in keys:
                    self._delete_draft_shot(folder, shot)
            return updated

    def discard_staged(self, draft: ManualDraft, keys: Collection[str]) -> ManualDraft:
        """Cancel a mark sheet: delete these staged screenshots (keys that
        are not staged, such as the mark's own, are left alone)."""
        with self._lock:
            dropped = tuple(shot for shot in draft.staged if shot.key in keys)
            if not dropped:
                return draft
            folder = self._draft_dir(draft.claim_token)
            updated = replace(
                draft,
                staged=tuple(shot for shot in draft.staged if shot.key not in keys),
            )
            if folder.is_dir():
                _write_json(folder / _DRAFT_FILE, _draft_to_json(updated))
                for shot in dropped:
                    self._delete_draft_shot(folder, shot)
            return updated

    def drop_staged(self, draft: ManualDraft) -> ManualDraft:
        """Delete every staged screenshot (no mark sheet is open any more)."""
        with self._lock:
            return self._drop_staged(self._draft_dir(draft.claim_token), draft)

    def _drop_staged(self, folder: Path, draft: ManualDraft) -> ManualDraft:
        if not draft.staged:
            return draft
        updated = replace(draft, staged=())
        if folder.is_dir():
            _write_json(folder / _DRAFT_FILE, _draft_to_json(updated))
            for shot in draft.staged:
                self._delete_draft_shot(folder, shot)
        return updated

    def remove_case_shot(self, draft: ManualDraft) -> ManualDraft:
        with self._lock:
            if draft.case_shot is None:
                return draft
            folder = self._draft_dir(draft.claim_token)
            updated = replace(draft, case_shot=None)
            if folder.is_dir():
                _write_json(folder / _DRAFT_FILE, _draft_to_json(updated))
                self._delete_draft_shot(folder, draft.case_shot)
            return updated

    @staticmethod
    def _delete_draft_shot(folder: Path, shot: SpoolShot) -> None:
        (folder / shot.file_name).unlink(missing_ok=True)
        (folder / f"{shot.key}{_THUMB_SUFFIX}").unlink(missing_ok=True)

    def read_thumbnail(self, claim_token: str, key: str) -> bytes | None:
        if not _is_shot_key(key):
            return None
        with self._lock:
            try:
                return (
                    self._draft_dir(claim_token) / f"{key}{_THUMB_SUFFIX}"
                ).read_bytes()
            except (OSError, SpoolError):
                return None

    def drop_draft(self, claim_token: str) -> None:
        with self._lock:
            shutil.rmtree(self._draft_dir(claim_token), ignore_errors=True)

    def drop_drafts_except(self, keep: str | None) -> None:
        """Delete the drafts of every claim but ``keep`` (the server's)."""
        with self._lock:
            for path in self._draft_dirs():
                if path.name != keep and not (path / _MANIFEST_FILE).exists():
                    logger.info("Discarding the draft of a released manual case")
                    shutil.rmtree(path, ignore_errors=True)

    def commit(
        self, draft: ManualDraft, payload: ManualCaseSubmitPayload, title: str
    ) -> SpooledSubmission:
        """Turn the draft into a hand-in (see the module docstring)."""
        with self._lock:
            folder = self._draft_dir(draft.claim_token)
            mimes = {shot.key: shot.mime for shot in draft.shots()}
            for shot in payload.shots:
                if (
                    shot.key not in mimes
                    or not (
                        folder / f"{shot.key}{_EXTENSIONS[mimes[shot.key]]}"
                    ).is_file()
                ):
                    raise SpoolError(
                        "A screenshot of this case is missing from the spool; "
                        "retake it."
                    )
            target = self._submission_dir(payload.submission_id)
            # An open mark sheet's screenshots are not part of the hand-in.
            for staged in draft.staged:
                self._delete_draft_shot(folder, staged)
            manifest: dict[str, object] = {
                "version": _MANIFEST_VERSION,
                "submission_id": payload.submission_id,
                "pivot_id": payload.pivot_id,
                "claim_token": payload.claim_token,
                "title": title[:_TITLE_MAX_CHARS],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "submit": json.loads(serialize_manual_case_submit(payload, "spool")),
                "mimes": {shot.key: mimes[shot.key].value for shot in payload.shots},
            }
            folder.mkdir(parents=True, exist_ok=True)
            _write_json(folder / _MANIFEST_FILE, manifest)
            self._outbox().mkdir(parents=True, exist_ok=True)
            os.replace(folder, target)
            self._clean_submission_dir(target, manifest)
            submission = self._load_submission(target)
            if submission is None:
                raise SpoolError("The hand-in could not be stored.")
            return submission

    @staticmethod
    def _clean_submission_dir(folder: Path, manifest: Mapping[str, object]) -> None:
        """Keep only the manifest, the accepted marker and announced shots."""
        mimes = manifest.get("mimes")
        keep = {_MANIFEST_FILE, _ACCEPTED_FILE}
        if isinstance(mimes, dict):
            for key, mime in mimes.items():
                try:
                    keep.add(f"{key}{_EXTENSIONS[ManualShotMime(str(mime))]}")
                except ValueError:
                    continue
        for child in folder.iterdir():
            if child.name in keep:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)

    # --- outbox ---------------------------------------------------------------

    def _submission_dirs(self) -> list[Path]:
        outbox = self._outbox()
        if not outbox.is_dir():
            return []
        return [
            path
            for path in outbox.iterdir()
            if path.is_dir() and not path.is_symlink() and _UUID_RE.fullmatch(path.name)
        ]

    def _load_submission(self, folder: Path) -> SpooledSubmission | None:
        manifest = _read_json(folder / _MANIFEST_FILE)
        if manifest is None:
            return None
        raw_submit = manifest.get("submit")
        raw_mimes = manifest.get("mimes")
        title = manifest.get("title", "")
        if not isinstance(raw_submit, dict) or not isinstance(raw_mimes, dict):
            return None
        try:
            payload = parse_manual_case_submit_payload(raw_submit)
            mimes = {
                str(key): ManualShotMime(str(mime)) for key, mime in raw_mimes.items()
            }
        except (ProtocolError, ValueError) as e:
            logger.warning("Ignoring an unreadable manual hand-in %s: %s", folder, e)
            return None
        if payload.submission_id != folder.name or any(
            shot.key not in mimes for shot in payload.shots
        ):
            return None
        remaining = tuple(
            shot.key
            for shot in payload.shots
            if (folder / f"{shot.key}{_EXTENSIONS[mimes[shot.key]]}").is_file()
        )
        return SpooledSubmission(
            submission_id=payload.submission_id,
            pivot_id=payload.pivot_id,
            claim_token=payload.claim_token,
            title=title if isinstance(title, str) else "",
            payload=payload,
            mimes=mimes,
            accepted=(folder / _ACCEPTED_FILE).exists(),
            remaining=remaining,
        )

    def submissions(self) -> tuple[SpooledSubmission, ...]:
        """Every readable hand-in, oldest first."""
        with self._lock:
            loaded: list[tuple[float, SpooledSubmission]] = []
            for folder in self._submission_dirs():
                submission = self._load_submission(folder)
                if submission is None:
                    continue
                try:
                    created = (folder / _MANIFEST_FILE).stat().st_mtime
                except OSError:
                    created = 0.0
                loaded.append((created, submission))
            loaded.sort(key=lambda item: item[0])
            return tuple(submission for _created, submission in loaded)

    def read_shot(
        self, submission_id: str, key: str
    ) -> tuple[bytes, ManualShotMime] | None:
        if not _is_shot_key(key):
            return None
        with self._lock:
            folder = self._submission_dir(submission_id)
            submission = self._load_submission(folder)
            if submission is None or key not in submission.mimes:
                return None
            mime = submission.mimes[key]
            try:
                return (folder / f"{key}{_EXTENSIONS[mime]}").read_bytes(), mime
            except OSError:
                return None

    def mark_accepted(self, submission_id: str) -> None:
        with self._lock:
            folder = self._submission_dir(submission_id)
            if folder.is_dir():
                _write_atomic(folder / _ACCEPTED_FILE, b"")
                self._finish_if_done(folder)

    def delete_shots(self, submission_id: str, keys: Collection[str]) -> None:
        """Delete acknowledged screenshots (and the hand-in once it is done)."""
        with self._lock:
            folder = self._submission_dir(submission_id)
            submission = self._load_submission(folder)
            if submission is None:
                return
            for key in keys:
                mime = submission.mimes.get(key)
                if mime is not None:
                    (folder / f"{key}{_EXTENSIONS[mime]}").unlink(missing_ok=True)
            self._finish_if_done(folder)

    def drop_submission(self, submission_id: str) -> None:
        """The server will never take this hand-in: delete it entirely."""
        with self._lock:
            shutil.rmtree(self._submission_dir(submission_id), ignore_errors=True)

    def _finish_if_done(self, folder: Path) -> None:
        submission = self._load_submission(folder)
        if submission is not None and submission.accepted and not submission.remaining:
            shutil.rmtree(folder, ignore_errors=True)
