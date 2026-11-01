"""The Test Runner window's Manual run snapshot (``manual_run_state``).

Read side only. The snapshot lists the project's running Manual run with its
cases (capped, run order), the claimed case with its verbatim steps from the
checkpoint the claim wrote (and its Ask AI questions), and every hand-in of
the project still waiting for screenshots, with the keys the server misses,
so a reconnected runner resumes its uploads. A runner with
MANUAL_STEP_GUIDE_V1 also gets each step's "How to do it" (one more query),
unless it would take the snapshot past MANUAL_RUN_STATE_SAFE_BYTES (estimated
from the text lengths, no extra serialization): then every step reads "none".
A constant number of queries (at most seven), whatever the number of cases.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import replace
from typing import Final

from agents.checkpoint import checkpoint_from_json
from agents.types import PersonHowToItem
from controller_client.protocol import (
    MANUAL_CASE_STATUS_MAX_CHARS,
    MANUAL_CASE_TITLE_MAX_CHARS,
    MANUAL_PENDING_UPLOADS_MAX,
    MANUAL_PRECONDITIONS_MAX_CHARS,
    MANUAL_RUN_STATE_BYTES_PER_CHAR,
    MANUAL_RUN_STATE_MAX_CASES,
    MANUAL_RUN_STATE_SAFE_BYTES,
    MANUAL_STEP_TEXT_MAX_CHARS,
    MANUAL_TESTRAIL_ID_MAX_CHARS,
    PERSON_STEP_HOW_TO_COMMAND_MAX_CHARS,
    PERSON_STEP_HOW_TO_MAX_ITEMS,
    PERSON_STEP_HOW_TO_SOURCE_MAX_CHARS,
    PERSON_STEP_HOW_TO_TEXT_MAX_CHARS,
    PERSON_STEP_LABEL_MAX_CHARS,
    ClientCapability,
    ManualActiveCasePayload,
    ManualCaseListItemPayload,
    ManualPendingUploadPayload,
    ManualRunInfoPayload,
    ManualRunStateMessage,
    ManualStepPayload,
    PersonHowToPayload,
    PersonStepQuestionPayload,
)
from projects.case_markup import markup_to_plain
from projects.manual_case_steps import manual_steps_from_checkpoint
from projects.manual_case_submit_services import manifest_keys
from projects.manual_step_guide_read import ManualGuideView, load_manual_guide
from projects.models import (
    ManualCaseSubmission,
    ManualUploadState,
    Project,
    RunHelpMode,
    TestRun,
    TestRunScreenshot,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.person_step_question_services import get_manual_ask_state
from projects.run_case_order import RUN_CASE_ORDERING

logger = logging.getLogger(__name__)

# JSON keys and punctuation counted per list entry (case, step, how-to item,
# question, missing key) in the size estimate.
_ENTRY_OVERHEAD_CHARS: Final = 64


def build_manual_run_state(
    *, project: Project, runner_capabilities: frozenset[str]
) -> ManualRunStateMessage:
    """The ``manual_run_state`` for ``project``'s MANUAL_RUN_V1 runner window.

    ``run`` is None when the project has no running Manual run (the window
    leaves the manual view); ``pending_uploads`` is listed either way. The
    claimed case is the run's one claim (claims belong to the project's one
    runner connection, so the snapshot is the same for every person at it).
    Questions and ``can_ask`` only for a runner that also
    advertised PERSON_STEP_ASSIST_V1; the steps' how-to and ``shell_label``
    only for one with MANUAL_STEP_GUIDE_V1 (older windows get the snapshot
    unchanged).
    """
    run = (
        TestRun.objects.filter(
            project_id=project.pk,
            help_mode=RunHelpMode.MANUAL,
            status=TestRunStatus.STARTED,
        )
        .only("id", "name")
        .order_by("-started_at", "-pk")
        .first()
    )
    pending = _pending_uploads(project.pk)
    if run is None:
        return ManualRunStateMessage(
            run=None, cases=(), active=None, pending_uploads=pending
        )
    rows = list(
        TestRunTestCase.objects.filter(test_run_id=run.pk)
        .order_by(*RUN_CASE_ORDERING)
        .values_list(
            "id",
            "status",
            "manual_claimed_label",
            "test_case__title",
            "test_case__testrail_id",
        )[:MANUAL_RUN_STATE_MAX_CASES]
    )
    cases = tuple(
        ManualCaseListItemPayload(
            pivot_id=pivot_id,
            title=str(title)[:MANUAL_CASE_TITLE_MAX_CHARS],
            testrail_id=str(testrail_id or "")[:MANUAL_TESTRAIL_ID_MAX_CHARS],
            status=str(status)[:MANUAL_CASE_STATUS_MAX_CHARS],
            claimed_label=str(label or "")[:PERSON_STEP_LABEL_MAX_CHARS],
        )
        for pivot_id, status, label, title, testrail_id in rows
    )
    guided = ClientCapability.MANUAL_STEP_GUIDE_V1.value in runner_capabilities
    return ManualRunStateMessage(
        run=ManualRunInfoPayload(
            run_id=run.pk,
            name=(run.name or f"Run #{run.pk}")[:PERSON_STEP_LABEL_MAX_CHARS],
        ),
        cases=cases,
        active=_active_case(
            run.pk,
            assist=ClientCapability.PERSON_STEP_ASSIST_V1.value in runner_capabilities,
            guided=guided,
            other_chars=(
                _text_chars(
                    text
                    for case in cases
                    for text in (case.title, case.testrail_id, case.claimed_label)
                )
                + _text_chars(key for upload in pending for key in upload.missing)
                if guided
                else 0
            ),
        ),
        pending_uploads=pending,
    )


def _active_case(
    test_run_id: int, *, assist: bool, guided: bool, other_chars: int
) -> ManualActiveCasePayload | None:
    """The run's claimed case with its verbatim steps (one query, plus one for
    its questions with ``assist`` and one for its guide with ``guided``, set
    only for a MANUAL_STEP_GUIDE_V1 runner). ``other_chars``: the estimated
    size of the rest of the snapshot (case list, pending uploads)."""
    claimed = (
        TestRunTestCase.objects.filter(
            test_run_id=test_run_id, status=TestRunTestCaseStatus.IN_PROGRESS
        )
        .exclude(manual_claim_token="")
        .select_related("test_case")
        .only(
            "id",
            "manual_claim_token",
            "manual_claimed_at",
            "execution_checkpoint",
            "test_case__title",
            "test_case__preconditions",
            # The rest of the case text the guide fingerprint covers.
            "test_case__steps",
            "test_case__expected",
        )
        .first()
    )
    if claimed is None:
        return None
    checkpoint = checkpoint_from_json(claimed.execution_checkpoint)
    steps = manual_steps_from_checkpoint(checkpoint) if checkpoint is not None else ()
    questions: tuple[PersonStepQuestionPayload, ...] = ()
    can_ask = False
    if assist:
        ask_state = get_manual_ask_state(claimed.pk, claimed.manual_claimed_at)
        # The window never shows who asked: no member names on the runner socket.
        questions = tuple(
            replace(view.to_payload(), asked_by="") for view in ask_state.questions
        )
        can_ask = ask_state.can_ask
    title = claimed.test_case.title[:MANUAL_CASE_TITLE_MAX_CHARS]
    preconditions = markup_to_plain(claimed.test_case.preconditions)[
        :MANUAL_PRECONDITIONS_MAX_CHARS
    ]
    plain_steps = [
        ManualStepPayload(
            num=step.num,
            text=markup_to_plain(step.text)[:MANUAL_STEP_TEXT_MAX_CHARS],
            expected=markup_to_plain(step.expected)[:MANUAL_STEP_TEXT_MAX_CHARS],
        )
        for step in steps
    ]
    guide = load_manual_guide(claimed) if guided else None
    if guide is not None and not _fits(
        other_chars
        + _text_chars((title, preconditions))
        + _text_chars(
            text for step in plain_steps for text in (step.text, step.expected)
        )
        + _text_chars(
            text
            for question in questions
            for text in (question.question, question.answer)
        ),
        guide,
        step_count=len(plain_steps),
    ):
        logger.warning(
            "Manual case %s: step guide left out of the runner snapshot (too large)",
            claimed.pk,
        )
        guide = None
    return ManualActiveCasePayload(
        pivot_id=claimed.pk,
        claim_token=claimed.manual_claim_token,
        title=title,
        preconditions=preconditions,
        steps=tuple(
            _step_payload(payload, guide, guided=guided) for payload in plain_steps
        ),
        claimed_at=(
            claimed.manual_claimed_at.isoformat()
            if claimed.manual_claimed_at is not None
            else ""
        ),
        questions=questions,
        can_ask=can_ask,
        shell_label=(
            guide.shell_label[:PERSON_STEP_LABEL_MAX_CHARS] if guide is not None else ""
        ),
    )


def _step_payload(
    payload: ManualStepPayload, guide: ManualGuideView | None, *, guided: bool
) -> ManualStepPayload:
    """``payload`` with its how-to for a guide-capable window (``guided``);
    unchanged otherwise. No guide to show reads ``none``."""
    if not guided:
        return payload
    if guide is None:
        return replace(payload, how_to_state="none")
    step_guide = guide.step(payload.num)
    return replace(
        payload,
        how_to=_how_to_payloads(step_guide.how_to),
        how_to_state=step_guide.state,
    )


def _text_chars(texts: Iterable[str]) -> int:
    """Estimated snapshot characters of ``texts``, one list entry each."""
    return sum(len(text) + _ENTRY_OVERHEAD_CHARS for text in texts)


def _fits(base_chars: int, guide: ManualGuideView, *, step_count: int) -> bool:
    """Whether the snapshot of ``base_chars`` stays within the safe budget
    with the how-to of its ``step_count`` steps (items at the protocol caps)."""
    how_to_chars = _text_chars(
        text
        for step in guide.steps[:step_count]
        for item in step.how_to[:PERSON_STEP_HOW_TO_MAX_ITEMS]
        for text in (
            item.text[:PERSON_STEP_HOW_TO_TEXT_MAX_CHARS],
            item.command[:PERSON_STEP_HOW_TO_COMMAND_MAX_CHARS],
            item.source_label[:PERSON_STEP_HOW_TO_SOURCE_MAX_CHARS],
        )
    )
    return (
        base_chars + how_to_chars
    ) * MANUAL_RUN_STATE_BYTES_PER_CHAR <= MANUAL_RUN_STATE_SAFE_BYTES


def _how_to_payloads(
    how_to: Sequence[PersonHowToItem],
) -> tuple[PersonHowToPayload, ...]:
    """The items cut to the protocol caps (a guide-capable window takes all 12:
    no fold, unlike ``runner_person_step_services._how_to_payloads``)."""
    return tuple(
        PersonHowToPayload(
            text=item.text[:PERSON_STEP_HOW_TO_TEXT_MAX_CHARS],
            command=item.command[:PERSON_STEP_HOW_TO_COMMAND_MAX_CHARS],
            source=item.source_label[:PERSON_STEP_HOW_TO_SOURCE_MAX_CHARS],
        )
        for item in how_to[:PERSON_STEP_HOW_TO_MAX_ITEMS]
    )


def _pending_uploads(project_id: int) -> tuple[ManualPendingUploadPayload, ...]:
    """The project's hand-ins still uploading, newest first (capped), with
    their missing keys (two queries; one when there are none)."""
    submissions = list(
        ManualCaseSubmission.objects.filter(
            test_run_test_case__test_run__project_id=project_id,
            # An ``incomplete`` one is resumed by the runner's own replay of
            # the hand-in, which answers its missing keys.
            upload_state=ManualUploadState.UPLOADING,
        )
        .order_by("-submitted_at", "-pk")
        .values_list("submission_id", "test_run_test_case_id", "shot_manifest")[
            :MANUAL_PENDING_UPLOADS_MAX
        ]
    )
    if not submissions:
        return ()
    keys_by_submission = [
        (submission_id, pivot_id, manifest_keys(manifest))
        for submission_id, pivot_id, manifest in submissions
    ]
    all_keys = {key for _sid, _pid, keys in keys_by_submission for key in keys}
    stored = set(
        TestRunScreenshot.objects.filter(
            test_run_test_case_id__in={pid for _sid, pid, _keys in keys_by_submission},
            manual_key__in=all_keys,
        ).values_list("test_run_test_case_id", "manual_key")
    )
    return tuple(
        ManualPendingUploadPayload(
            submission_id=str(submission_id),
            missing=tuple(key for key in keys if (pivot_id, key) not in stored),
        )
        for submission_id, pivot_id, keys in keys_by_submission
    )
