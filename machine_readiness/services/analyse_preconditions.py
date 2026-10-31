"""Stage-0 (S0) extraction: one cached LLM pass per distinct precondition text.

Plan: docs/plan/feat-machine-setup-intake.md › D4, Services ›
analyse_preconditions.py. A text is identified by ``TestCase.preconditions_md5``
(MD5 of the trimmed text); once a ``PreconditionAnalysis`` row exists for it,
it is never sent again until the text changes. Texts are batched by
``STAGE0_ANALYSIS_BATCH_CHARS``; each batch is one call that must answer with
the single ``record_stage0`` tool, validated by ``stage0_rules``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Max
from django.db.models.functions import Length
from django.utils import timezone

from agents.exceptions import AIServiceError
from agents.services.llm_client import send_chat_completion
from agents.services.llm_config import build_llm_config
from agents.types import ChatMessage, LLMConfig
from machine_readiness import project_locks
from machine_readiness.events import broadcast_readiness
from machine_readiness.exceptions import Stage0AnalysisBusyError
from machine_readiness.models import (
    ANALYSIS_UNCLEAR_MAX_LENGTH,
    PROJECT_MAX_REQUIREMENTS,
    PreconditionAnalysis,
    RequirementOrigin,
    Stage0Requirement,
)
from machine_readiness.stage0_prompts import (
    RECORD_STAGE0_TOOL,
    RECORD_STAGE0_TOOL_NAME,
    build_stage0_system_prompt,
    build_stage0_user_prompt,
)
from machine_readiness.stage0_rules import parse_stage0_payload
from machine_readiness.types import Stage0BatchResult, Stage0RequirementData
from projects.models import TestCase

logger = logging.getLogger(__name__)

# MD5 of the empty string: the generated column's value for blank
# preconditions (only spaces are trimmed by Postgres TRIM).
EMPTY_PRECONDITIONS_MD5 = "d41d8cd98f00b204e9800998ecf8427e"
# One extraction call; a batch is at most STAGE0_ANALYSIS_BATCH_CHARS of text.
CALL_TIMEOUT_SECONDS = 300
# The project's analysis lock covers one call plus its writes and is
# refreshed before every batch, so it is never held longer than about six
# minutes after its holder died (a SIGTERM'd setup turn, a killed worker),
# however many calls the analysis was allowed.
ANALYSIS_LOCK_TTL_SECONDS = CALL_TIMEOUT_SECONDS + 60
# The Celery task: every call may take CALL_TIMEOUT_SECONDS; the writes add
# little.
ANALYSIS_TASK_SOFT_LIMIT_SECONDS = (
    max(int(settings.STAGE0_ANALYSIS_MAX_CALLS), 1) * CALL_TIMEOUT_SECONDS + 60
)
ANALYSIS_BUSY_MESSAGE = (
    "The preconditions are being analysed already; try again in a few minutes."
)
_CUT_NOTE = "\n[… text cut: too long for one analysis call]"
# Problems kept in the outcome across every batch.
_MAX_OUTCOME_PROBLEMS = 20


@dataclass(frozen=True)
class AnalysisOutcome:
    """What one analysis did; ``remaining`` texts are still unanalysed."""

    # LLM calls made (each is one batch).
    calls: int = 0
    # Distinct texts recorded, including the empty ones.
    analysed: int = 0
    # Of those, texts that need nothing on the machine.
    none_needed: int = 0
    requirements_created: int = 0
    remaining: int = 0
    # Entries the model got wrong and were dropped (safe to show).
    problems: tuple[str, ...] = ()
    # Why the analysis stopped early (an AI service error), "" otherwise.
    error: str = ""


@dataclass(frozen=True)
class _PendingText:
    md5: str
    case_count: int
    length: int


@dataclass
class _Catalog:
    """The project's requirements: key -> (id, title), kept up to date."""

    rows: dict[str, tuple[int, str]] = field(default_factory=dict)

    def keys_and_titles(self) -> list[tuple[str, str]]:
        return [(key, title) for key, (_, title) in sorted(self.rows.items())]


def analysis_running(project_id: int) -> bool:
    """Whether an analysis of the project is queued or holds the lock."""
    return project_locks.is_locked(project_locks.ANALYSIS_LOCK_PREFIX, project_id)


def analyse_preconditions(
    *,
    project_id: int,
    max_calls: int,
    lock_token: str | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> AnalysisOutcome:
    """Analyse the project's unanalysed precondition texts, at most ``max_calls``
    LLM calls. Raises ``Stage0AnalysisBusyError`` while another runs.

    ``lock_token``: the lock ``request_analysis`` took for the task (else a
    new one is taken). ``should_stop`` is asked before each batch (a setup
    turn's Stop); the batches done so far are kept.
    """
    lock = (
        project_locks.claim(
            project_locks.ANALYSIS_LOCK_PREFIX,
            project_id,
            lock_token,
            ttl=ANALYSIS_LOCK_TTL_SECONDS,
        )
        if lock_token is not None
        else project_locks.acquire(
            project_locks.ANALYSIS_LOCK_PREFIX,
            project_id,
            ttl=ANALYSIS_LOCK_TTL_SECONDS,
        )
    )
    if lock is None:
        raise Stage0AnalysisBusyError(ANALYSIS_BUSY_MESSAGE)
    try:
        return _analyse(
            project_id=project_id,
            max_calls=max_calls,
            lock=lock,
            should_stop=should_stop,
        )
    finally:
        project_locks.release(lock)


def _analyse(
    *,
    project_id: int,
    max_calls: int,
    lock: project_locks.ProjectLock,
    should_stop: Callable[[], bool] | None,
) -> AnalysisOutcome:
    pending = _pending_texts(project_id)
    if not pending:
        return AnalysisOutcome()
    batch_chars = max(int(settings.STAGE0_ANALYSIS_BATCH_CHARS), 1000)
    empty = [text.md5 for text in pending if text.md5 == EMPTY_PRECONDITIONS_MD5]
    planned = _plan_batches(
        [text for text in pending if text.md5 != EMPTY_PRECONDITIONS_MD5],
        batch_chars=batch_chars,
        max_calls=max(max_calls, 0),
    )
    texts = _load_texts(project_id, [md5 for batch in planned for md5 in batch])
    blank = [md5 for md5, text in texts.items() if not text.strip()]
    none_needed = _record_none_needed(project_id, [*empty, *blank])
    analysed = none_needed
    batches = [
        [md5 for md5 in batch if texts.get(md5, "").strip()] for batch in planned
    ]
    batches = [batch for batch in batches if batch]

    calls = 0
    created = 0
    problems: list[str] = []
    error = ""
    if batches:
        catalog = _load_catalog(project_id)
        config = build_llm_config()
        system_prompt = build_stage0_system_prompt()
        for batch in batches:
            if should_stop is not None and should_stop():
                error = "Stopped before every batch was analysed."
                break
            if not project_locks.refresh(lock, ttl=ANALYSIS_LOCK_TTL_SECONDS):
                # The lock expired and another analysis took it: leave the
                # rest to that one rather than analyse the same texts twice.
                error = "Another analysis took over."
                break
            refs = {f"T{index}": md5 for index, md5 in enumerate(batch, start=1)}
            calls += 1
            try:
                result = _extract(
                    config,
                    system_prompt,
                    refs=refs,
                    texts=texts,
                    catalog=catalog,
                    batch_chars=batch_chars,
                )
            except AIServiceError as exc:
                logger.warning(
                    "stage0 analysis stopped: project=%s kind=%s", project_id, exc.kind
                )
                error = exc.message
                break
            problems.extend(result.problems)
            batch_created, batch_analysed, batch_none = _store_batch(
                project_id, result, refs=refs, catalog=catalog
            )
            created += batch_created
            analysed += batch_analysed
            none_needed += batch_none
            _broadcast_progress(
                project_id, analysed=analysed, remaining=len(pending) - analysed
            )

    if problems:
        logger.info(
            "stage0 analysis problems: project=%s count=%d first=%s",
            project_id,
            len(problems),
            problems[0],
        )
    remaining = max(len(pending) - analysed, 0)
    _broadcast_progress(project_id, analysed=analysed, remaining=remaining, done=True)
    return AnalysisOutcome(
        calls=calls,
        analysed=analysed,
        none_needed=none_needed,
        requirements_created=created,
        remaining=remaining,
        problems=tuple(problems[:_MAX_OUTCOME_PROBLEMS]),
        error=error,
    )


def _broadcast_progress(
    project_id: int, *, analysed: int, remaining: int, done: bool = False
) -> None:
    broadcast_readiness(
        project_id,
        {
            "event": "analysis",
            "analysed": analysed,
            "remaining": max(remaining, 0),
            "done": done,
        },
    )


def _pending_texts(project_id: int) -> list[_PendingText]:
    """Distinct unanalysed md5 values, most shared first (one query)."""
    analysed = PreconditionAnalysis.objects.filter(project_id=project_id).values(
        "preconditions_md5"
    )
    rows = (
        TestCase.objects.filter(project_id=project_id)
        .exclude(preconditions_md5__in=analysed)
        .values("preconditions_md5")
        .annotate(case_count=Count("id"), length=Max(Length("preconditions")))
        .order_by("-case_count", "preconditions_md5")
    )
    return [
        _PendingText(
            md5=row["preconditions_md5"],
            case_count=row["case_count"],
            length=row["length"] or 0,
        )
        for row in rows
    ]


def _plan_batches(
    pending: Sequence[_PendingText], *, batch_chars: int, max_calls: int
) -> list[list[str]]:
    """Group md5 values into at most ``max_calls`` batches of ``batch_chars``.

    Every batch holds at least one text (a longer one is cut when sent).
    """
    batches: list[list[str]] = []
    used = 0
    for text in pending:
        size = min(text.length, batch_chars)
        if batches and used + size <= batch_chars:
            batches[-1].append(text.md5)
            used += size
            continue
        if len(batches) >= max_calls:
            # A shorter text further down may still fit the last batch.
            continue
        batches.append([text.md5])
        used = size
    return batches


def _load_texts(project_id: int, md5s: Sequence[str]) -> dict[str, str]:
    """One text per md5 (one query)."""
    if not md5s:
        return {}
    rows = (
        TestCase.objects.filter(project_id=project_id, preconditions_md5__in=md5s)
        .order_by("preconditions_md5")
        .distinct("preconditions_md5")
        .values_list("preconditions_md5", "preconditions")
    )
    return {md5: text for md5, text in rows}


def _load_catalog(project_id: int) -> _Catalog:
    rows = Stage0Requirement.objects.filter(project_id=project_id).values_list(
        "key", "id", "title"
    )
    return _Catalog(rows={key: (pk, title) for key, pk, title in rows})


def _extract(
    config: LLMConfig,
    system_prompt: str,
    *,
    refs: Mapping[str, str],
    texts: Mapping[str, str],
    catalog: _Catalog,
    batch_chars: int,
) -> Stage0BatchResult:
    batch_texts = [
        (ref, _clip_text(texts[md5].strip(), batch_chars)) for ref, md5 in refs.items()
    ]
    response = send_chat_completion(
        config,
        (
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(
                role="user",
                content=build_stage0_user_prompt(
                    batch_texts, catalog.keys_and_titles()
                ),
            ),
        ),
        (RECORD_STAGE0_TOOL,),
        tool_choice="required",
        timeout_seconds=CALL_TIMEOUT_SECONDS,
    )
    payload = next(
        (
            call.arguments
            for call in response.message.tool_calls or ()
            if call.tool_name == RECORD_STAGE0_TOOL_NAME
        ),
        None,
    )
    if payload is None:
        return Stage0BatchResult(
            problems=(
                "The model did not call record_stage0; the batch stays unanalysed.",
            )
        )
    return parse_stage0_payload(
        payload,
        text_refs=refs.keys(),
        catalog_keys=catalog.rows.keys(),
        project_requirement_count=len(catalog.rows),
    )


def _clip_text(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + _CUT_NOTE


def _record_none_needed(project_id: int, md5s: Sequence[str]) -> int:
    if not md5s:
        return 0
    now = timezone.now()
    PreconditionAnalysis.objects.bulk_create(
        [
            PreconditionAnalysis(
                project_id=project_id,
                preconditions_md5=md5,
                analysed_at=now,
                none_needed=True,
            )
            for md5 in md5s
        ],
        ignore_conflicts=True,
    )
    return len(md5s)


def _store_batch(
    project_id: int,
    result: Stage0BatchResult,
    *,
    refs: Mapping[str, str],
    catalog: _Catalog,
) -> tuple[int, int, int]:
    """Create the new requirements and write the batch's analyses.

    Returns (requirements created, texts analysed, of which none needed).
    Existing requirements are never redefined here: a person's edit wins.
    """
    if not result.texts:
        return 0, 0, 0
    with transaction.atomic():
        created = _create_requirements(project_id, result.requirements, catalog)
        now = timezone.now()
        analyses: list[PreconditionAnalysis] = []
        keys_by_md5: dict[str, list[str]] = {}
        none_count = 0
        for text in result.texts:
            md5 = refs[text.ref]
            keys = [key for key in text.requirement_keys if key in catalog.rows]
            unclear = text.unclear
            dropped = [key for key in text.requirement_keys if key not in catalog.rows]
            if dropped:
                note = "Not recorded (requirement limit): " + ", ".join(dropped[:5])
                unclear = f"{unclear} {note}".strip() if unclear else note
            keys_by_md5[md5] = keys
            none_count += 1 if text.none_needed else 0
            analyses.append(
                PreconditionAnalysis(
                    project_id=project_id,
                    preconditions_md5=md5,
                    analysed_at=now,
                    unclear=unclear[:ANALYSIS_UNCLEAR_MAX_LENGTH],
                    none_needed=text.none_needed,
                )
            )
        saved = PreconditionAnalysis.objects.bulk_create(
            analyses,
            update_conflicts=True,
            unique_fields=["project", "preconditions_md5"],
            update_fields=["analysed_at", "unclear", "none_needed"],
        )
        through = PreconditionAnalysis.requirements.through
        analysis_ids = [analysis.pk for analysis in saved]
        # A row another writer created meanwhile is overwritten as a whole.
        through.objects.filter(preconditionanalysis_id__in=analysis_ids).delete()
        through.objects.bulk_create(
            [
                through(
                    preconditionanalysis_id=analysis.pk,
                    stage0requirement_id=catalog.rows[key][0],
                )
                for analysis in saved
                for key in keys_by_md5[analysis.preconditions_md5]
            ],
            ignore_conflicts=True,
        )
    return created, len(analyses), none_count


def _create_requirements(
    project_id: int,
    requirements: Sequence[Stage0RequirementData],
    catalog: _Catalog,
) -> int:
    """``get_or_create`` by key in bulk, within the per-project cap.

    Updates ``catalog`` with every key of ``requirements`` that now exists.
    """
    new = [item for item in requirements if item.key not in catalog.rows]
    if not new:
        return 0
    room = (
        PROJECT_MAX_REQUIREMENTS
        - Stage0Requirement.objects.filter(project_id=project_id).count()
    )
    new = new[: max(room, 0)]
    if not new:
        return 0
    started = timezone.now()
    Stage0Requirement.objects.bulk_create(
        [
            Stage0Requirement(
                project_id=project_id,
                key=item.key,
                title=item.title,
                kind=item.kind,
                check_target=item.check_target,
                install_hint=item.install_hint,
                conflicts_with=list(item.conflicts_with),
                origin=RequirementOrigin.ANALYSIS,
            )
            for item in new
        ],
        # A key created meanwhile (setup agent, person) is kept as it is.
        ignore_conflicts=True,
    )
    rows = Stage0Requirement.objects.filter(
        project_id=project_id, key__in=[item.key for item in new]
    ).values_list("key", "id", "title", "created_at")
    created = 0
    for key, pk, title, created_at in rows:
        catalog.rows[key] = (pk, title)
        if created_at >= started:
            created += 1
    return created
