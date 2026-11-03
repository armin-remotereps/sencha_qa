"""What the classifier sees of each run case, and the hash that lets an
unchanged case keep its row.

``build_case_drafts`` reads cases in id chunks with ``.only()`` columns (the
log tail through ``Right`` in SQL, never the whole log), their candidate
screenshots and, for manual cases, the current hand-in's shot keys: about
four queries per chunk. ``load_case_inputs`` adds one query for the chosen
screenshots and reads and downscales their files; call it only for the cases
that need an AI call. No ORM may run in the classifier's worker threads, so
both run in the caller's thread.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from django.conf import settings
from django.db.models.functions import Right
from PIL import Image, UnidentifiedImageError

from agents.checkpoint import checkpoint_from_json
from agents.triage_types import TriageCaseInput, TriageShot
from projects.manual_run_policy import is_manual_checkpoint
from projects.models import (
    ManualCaseSubmission,
    ManualUploadState,
    TestCase,
    TestRunScreenshot,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.testrail_evidence import (
    OVERLAY_TOOL_NAME,
    EvidenceShot,
    ManualEvidenceShot,
    select_evidence,
    select_manual_evidence,
)
from projects.testrail_results_services import pivot_result_text, pivot_step_breakdown
from run_triage.case_refs import pivot_ref
from run_triage.case_scope import is_untested
from run_triage.taxonomy import TRIAGE_PROMPT_VERSION
from run_triage.tool_text import cut

logger = logging.getLogger(__name__)

# Cases read per query round.
BUILD_CHUNK_SIZE: Final = 100
# Mirrors ``machine_setup.services.case_evidence_tools.SCREENSHOT_MAX_BYTES``
# (the cap of ``ask_case_screenshot``); run_triage may not import machine_setup
# services, so keep both equal.
SCREENSHOT_MAX_BYTES: Final = 8 * 1024 * 1024
_JPEG_QUALITY: Final = 85
_LOG_TAIL: Final = "triage_log_tail"
_EVIDENCE_STATUSES: Final = (
    TestRunTestCaseStatus.FAILED,
    TestRunTestCaseStatus.BLOCKED,
)

_PIVOT_FIELDS: Final = (
    "id",
    "test_run",
    "status",
    "result",
    "blocked_reason",
    "failed_by",
    "retest_from_status",
    "retest_note",
    "retest_marked_by",
    "retest_marked_by__first_name",
    "retest_marked_by__last_name",
    "retest_marked_by__email",
    "finished_at",
    "segment_started_at",
    "execution_checkpoint",
    "testrail_source_result_id",
    "testrail_test_id",
    "testrail_source_test_id",
    "test_case__title",
    "test_case__preconditions",
    "test_case__steps",
    "test_case__expected",
)


@dataclass(frozen=True)
class CaseDraft:
    """One case's classifier text and chosen screenshot ids (no image bytes)."""

    pivot_id: int
    ref: str
    status: str
    finished_at: datetime | None
    untested: bool
    title: str
    status_label: str
    case_text: str
    result_text: str
    log_tail: str
    # (screenshot id, step number) of the screenshots sent, in order.
    shots: tuple[tuple[int, int | None], ...]
    input_hash: str


def build_case_drafts(test_run_id: int, pivot_ids: Sequence[int]) -> list[CaseDraft]:
    """Drafts of these cases of the run, in the order of ``pivot_ids``."""
    drafts: dict[int, CaseDraft] = {}
    ids = list(pivot_ids)
    for start in range(0, len(ids), BUILD_CHUNK_SIZE):
        chunk = ids[start : start + BUILD_CHUNK_SIZE]
        for draft in _build_chunk(test_run_id, chunk):
            drafts[draft.pivot_id] = draft
    return [drafts[pivot_id] for pivot_id in ids if pivot_id in drafts]


def load_case_inputs(drafts: Sequence[CaseDraft]) -> list[TriageCaseInput]:
    """Classifier inputs for ``drafts`` with their screenshots read and
    downscaled; a screenshot that cannot be read is left out."""
    images = _load_images(shot_id for draft in drafts for shot_id, _ in draft.shots)
    inputs: list[TriageCaseInput] = []
    for draft in drafts:
        shots = tuple(
            TriageShot(
                screenshot_id=shot_id,
                step_num=step_num,
                base64_data=images[shot_id][0],
                media_type=images[shot_id][1],
            )
            for shot_id, step_num in draft.shots
            if shot_id in images
        )
        inputs.append(
            TriageCaseInput(
                ref=draft.ref,
                title=draft.title,
                status_label=draft.status_label,
                case_text=draft.case_text,
                result_text=draft.result_text,
                log_tail=draft.log_tail,
                screenshots=shots,
            )
        )
    return inputs


def case_text_of(test_case: TestCase) -> str:
    """The case's preconditions, steps and expected result as plain text."""
    parts = [f"Title: {test_case.title}"]
    for label, value in (
        ("Preconditions", test_case.preconditions),
        ("Steps", test_case.steps),
        ("Expected result", test_case.expected),
    ):
        if value.strip():
            parts.append(f"{label}:\n{value.strip()}")
    return "\n\n".join(parts)


def verdict_text_of(pivot: TestRunTestCase) -> str:
    """The verdict text and the step breakdown, as TestRail would get them."""
    result = pivot_result_text(pivot).strip()
    breakdown = pivot_step_breakdown(pivot).strip()
    return "\n\n".join(part for part in (result, breakdown) if part)


def status_label_of(pivot: TestRunTestCase) -> str:
    label = str(pivot.get_status_display())
    if pivot.status == TestRunTestCaseStatus.NEEDS_RETEST and pivot.retest_from_status:
        earlier = str(TestRunTestCaseStatus(pivot.retest_from_status).label)
        return f"{label} (marked from {earlier})"
    if pivot.status == TestRunTestCaseStatus.FAILED and pivot.failed_by:
        return f"{label} ({pivot.get_failed_by_display()})"
    if is_untested(pivot.status):
        return f"{label} (untested: it stopped with a partial result)"
    return label


def evidence_status(pivot: TestRunTestCase) -> str:
    """The status whose screenshots are relevant: Failed or Blocked, a Needs
    re-test case by its earlier verdict; "" for any other case."""
    if pivot.status in _EVIDENCE_STATUSES:
        return pivot.status
    if (
        pivot.status == TestRunTestCaseStatus.NEEDS_RETEST
        and pivot.retest_from_status in _EVIDENCE_STATUSES
    ):
        return pivot.retest_from_status
    return ""


def _build_chunk(test_run_id: int, pivot_ids: list[int]) -> list[CaseDraft]:
    queryset = (
        TestRunTestCase.objects.filter(test_run_id=test_run_id, id__in=pivot_ids)
        .select_related("test_case", "retest_marked_by")
        .only(*_PIVOT_FIELDS)
    )
    tail_chars = settings.RUN_TRIAGE_LOG_TAIL_CHARS
    if tail_chars > 0:
        queryset = queryset.annotate(**{_LOG_TAIL: Right("logs", tail_chars)})
    pivots = list(queryset)
    shots = _select_shots(pivots)
    return [_draft(pivot, shots.get(pivot.id, ())) for pivot in pivots]


def _draft(
    pivot: TestRunTestCase, shots: tuple[tuple[int, int | None], ...]
) -> CaseDraft:
    budget = settings.RUN_TRIAGE_CASE_INPUT_MAX_CHARS
    case_text = cut(case_text_of(pivot.test_case), budget // 2)
    result_text = cut(verdict_text_of(pivot), max(budget - len(case_text), 0))
    raw_tail = getattr(pivot, _LOG_TAIL, "")
    log_tail = raw_tail if isinstance(raw_tail, str) else ""
    title = pivot.test_case.title
    status_label = status_label_of(pivot)
    return CaseDraft(
        pivot_id=pivot.id,
        ref=pivot_ref(pivot),
        status=pivot.status,
        finished_at=pivot.finished_at,
        untested=is_untested(pivot.status),
        title=title,
        status_label=status_label,
        case_text=case_text,
        result_text=result_text,
        log_tail=log_tail,
        shots=shots,
        input_hash=_input_hash(
            title=title,
            status_label=status_label,
            case_text=case_text,
            result_text=result_text,
            log_tail=log_tail,
            shot_ids=[shot_id for shot_id, _ in shots],
        ),
    )


def _input_hash(
    *,
    title: str,
    status_label: str,
    case_text: str,
    result_text: str,
    log_tail: str,
    shot_ids: list[int],
) -> str:
    payload = json.dumps(
        [
            TRIAGE_PROMPT_VERSION,
            title,
            status_label,
            case_text,
            result_text,
            log_tail,
            shot_ids,
        ],
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _select_shots(
    pivots: list[TestRunTestCase],
) -> dict[int, tuple[tuple[int, int | None], ...]]:
    """The screenshots sent with each case: two queries at most."""
    max_shots = settings.RUN_TRIAGE_SHOTS_PER_CASE
    if max_shots <= 0:
        return {}
    statuses = {
        pivot.id: status for pivot in pivots if (status := evidence_status(pivot))
    }
    if not statuses:
        return {}
    by_id = {pivot.id: pivot for pivot in pivots}
    manual_ids = [
        pivot_id
        for pivot_id in statuses
        if is_manual_checkpoint(by_id[pivot_id].execution_checkpoint)
    ]
    manual_keys = _current_manual_shot_keys(manual_ids)
    agent_shots: dict[int, list[EvidenceShot]] = {pid: [] for pid in statuses}
    manual_shots: dict[int, list[ManualEvidenceShot]] = {pid: [] for pid in manual_ids}
    steps: dict[int, int | None] = {}
    rows = (
        TestRunScreenshot.objects.filter(test_run_test_case_id__in=list(statuses))
        .exclude(tool_name=OVERLAY_TOOL_NAME)
        .order_by("created_at", "id")
        .values_list(
            "id",
            "test_run_test_case_id",
            "attempt_id",
            "step_num",
            "tool_name",
            "manual_key",
        )
    )
    for shot_id, pivot_id, attempt_id, step_num, tool_name, manual_key in rows:
        steps[shot_id] = step_num
        if pivot_id in manual_shots:
            if manual_key and manual_key in manual_keys.get(pivot_id, frozenset()):
                manual_shots[pivot_id].append(
                    ManualEvidenceShot(
                        id=shot_id, tool_name=tool_name, step_num=step_num
                    )
                )
            continue
        agent_shots[pivot_id].append(
            EvidenceShot(id=shot_id, attempt_id=attempt_id, step_num=step_num)
        )
    chosen: dict[int, tuple[tuple[int, int | None], ...]] = {}
    for pivot_id, status in statuses.items():
        if pivot_id in manual_shots:
            ids = select_manual_evidence(
                status=status, shots=manual_shots[pivot_id], max_shots=max_shots
            )
        else:
            ids = select_evidence(
                status=status,
                checkpoint=checkpoint_from_json(by_id[pivot_id].execution_checkpoint),
                shots=agent_shots[pivot_id],
                per_step=1,
                max_shots=max_shots,
            )
        chosen[pivot_id] = tuple((shot_id, steps.get(shot_id)) for shot_id in ids)
    return chosen


def _current_manual_shot_keys(pivot_ids: list[int]) -> dict[int, frozenset[str]]:
    """The shot keys of each manual case's current hand-in (one query).

    Same rule as ``projects.manual_current_submission.current_manual_shot_keys``
    (the newest hand-in that Re-run or Reset did not supersede); keep both
    aligned.
    """
    if not pivot_ids:
        return {}
    rows = (
        ManualCaseSubmission.objects.filter(test_run_test_case_id__in=pivot_ids)
        .exclude(upload_state=ManualUploadState.SUPERSEDED)
        .order_by("test_run_test_case_id", "-submitted_at", "-id")
        .distinct("test_run_test_case_id")
        .values_list("test_run_test_case_id", "shot_manifest")
    )
    keys: dict[int, frozenset[str]] = {}
    for pivot_id, manifest in rows:
        entries = manifest if isinstance(manifest, list) else []
        keys[pivot_id] = frozenset(
            entry["key"]
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("key"), str)
        )
    return keys


def _load_images(shot_ids: Iterable[int]) -> dict[int, tuple[str, str]]:
    """(base64 data, media type) of each readable screenshot (one query)."""
    ids = sorted(set(shot_ids))
    if not ids:
        return {}
    images: dict[int, tuple[str, str]] = {}
    for shot in TestRunScreenshot.objects.filter(id__in=ids).only("id", "image"):
        content = _read_file(shot)
        if content is None:
            continue
        encoded = _downscaled(content)
        if encoded is not None:
            images[shot.id] = encoded
    return images


def _read_file(shot: TestRunScreenshot) -> bytes | None:
    """The file's bytes; None when missing, empty or over the size cap."""
    if not shot.image.name:
        return None
    try:
        with shot.image.open("rb") as handle:
            content: bytes = handle.read(SCREENSHOT_MAX_BYTES + 1)
    except (FileNotFoundError, OSError):
        logger.warning("Case analysis: screenshot %s is unreadable", shot.id)
        return None
    if not content or len(content) > SCREENSHOT_MAX_BYTES:
        return None
    return content


def _downscaled(content: bytes) -> tuple[str, str] | None:
    """A JPEG of the image with its long edge at most ``RUN_TRIAGE_SHOT_MAX_EDGE``."""
    edge = settings.RUN_TRIAGE_SHOT_MAX_EDGE
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.thumbnail((edge, edge))
            output = io.BytesIO()
            image.convert("RGB").save(output, format="JPEG", quality=_JPEG_QUALITY)
    except (
        Image.DecompressionBombError,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ):
        return None
    return base64.b64encode(output.getvalue()).decode("ascii"), "image/jpeg"
