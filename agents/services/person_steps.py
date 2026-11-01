"""Run a test step through a person (Human in the loop).

``prepare_person_step`` words a plan step (or the state restore before a
resumed case) for a person in one tool-free LLM call; ``confirm_person_step``
checks the person's report with a sub-agent restricted to a screenshot and
read-only shell checks. The orchestrator decides when to call them; this module
knows nothing of run modes, checkpoints or the ORM.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Final, Literal

from django.conf import settings

from agents.how_to_source import PROFILE_SOURCE_LABEL, SOURCE_LABEL_MAX_CHARS
from agents.json_values import positive_int
from agents.os_shell import get_os_name
from agents.secret_redaction import redact_secrets
from agents.services.llm_client import send_chat_completion
from agents.services.person_step_prompts import (
    COMMAND_MAX_CHARS,
    EXPECTED_RESULT_MAX_CHARS,
    HOW_TO_COMMAND_MAX_CHARS,
    HOW_TO_MAX_ITEMS,
    HOW_TO_TEXT_MAX_CHARS,
    INSTRUCTION_MAX_CHARS,
    build_confirm_sub_task,
    build_mark_evidence_sub_task,
    build_prepare_system_prompt,
    build_prepare_user_prompt,
    build_restore_user_prompt,
    cap_text,
    fallback_hand_off,
    format_person_evidence,
    strip_executor_vocabulary,
)
from agents.services.sub_agent import run_sub_agent
from agents.services.tools_controller import COMMAND_LOG_PREFIX, capture_screenshot
from agents.types import (
    AgentConfig,
    BrowserSelection,
    ChatMessage,
    CheckpointPersonStep,
    LLMConfig,
    LogCallback,
    PersonGuideContext,
    PersonHowToItem,
    PersonHowToSourceKind,
    PersonStepHandOff,
    ScreenshotCallback,
    SubTask,
    SubTaskResult,
)

logger = logging.getLogger(__name__)

# A screenshot and read-only shell checks; no send_command_input (no
# interactive input) and no input, launch, close or browser tools.
CONFIRM_TOOL_NAMES: Final = frozenset(
    {"take_screenshot", "execute_command", "wait_for_command"}
)

_JSON_BLOCK: Final = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_QUOTES: Final = "`'\""
# Where a case states a whole command: an inline code span or a quoted string
# (straight or curly quotes), on one line.
_QUOTED_SPAN: Final = re.compile(
    r"`([^`\n]+)`|\"([^\"\n]+)\"|'([^'\n]+)'|\u201c([^\u201d\n]+)\u201d"
    r"|\u2018([^\u2019\n]+)\u2019"
)
# A list marker ("- ", "* ", "1. ", "2) ") or a shell prompt ("$ ", "> ")
# before a command written on its own line.
_LINE_PREFIX: Final = re.compile(r"^(?:[-*\u2022]|\d+[.)]|[$>])\s+")


def prepare_person_step(
    llm: LLMConfig,
    sub_task: SubTask,
    *,
    step_num: int,
    total_steps: int,
    task_description: str,
    system_info: dict[str, object] | None,
    restore: bool = False,
    guide: PersonGuideContext | None = None,
) -> PersonStepHandOff:
    """The hand-off of ``sub_task`` (or, with ``restore``, of the restore task).

    ``guide`` grounds the "How to do it" in the project's artifacts profile,
    the case's research notes and knowledge base excerpts (fenced as data);
    an item may cite only those, and the server builds its source label.
    Without it the prompt is the ungrounded one.

    An unusable answer falls back to the step's own text as an interactive
    step; an ``AIServiceError`` (the provider failed) propagates as a fault.
    """
    os_name = get_os_name(system_info)
    user_prompt = (
        build_restore_user_prompt(sub_task, guide=guide)
        if restore
        else build_prepare_user_prompt(
            sub_task,
            task_description=task_description,
            step_num=step_num,
            total_steps=total_steps,
            guide=guide,
        )
    )
    response = send_chat_completion(
        llm,
        (
            ChatMessage(
                role="system",
                content=build_prepare_system_prompt(
                    os_name=os_name, restore=restore, guide=guide
                ),
            ),
            ChatMessage(role="user", content=user_prompt),
        ),
    )
    raw = response.message.content if isinstance(response.message.content, str) else ""
    hand_off = parse_hand_off(
        raw,
        step_num=step_num,
        task_description=task_description,
        restore=restore,
        allowed_items=(
            guide.allowed_items
            if guide is not None and guide.allowed_items is not None
            else {}
        ),
        profile_given=guide is not None and bool(guide.artifacts_profile.strip()),
    )
    if hand_off is None:
        logger.warning(
            "Person step %d: unusable prepare answer, using the plan text", step_num
        )
        hand_off = fallback_hand_off(sub_task, step_num=step_num, restore=restore)
    if restore:
        # A restore covers earlier steps; a case step label there would mislead.
        return hand_off
    # The refs belong to the plan step; the model never writes them.
    return replace(hand_off, case_steps=sub_task.case_steps)


def parse_hand_off(
    raw: str,
    *,
    step_num: int,
    task_description: str,
    restore: bool,
    allowed_items: Mapping[int, str] | None = None,
    profile_given: bool = False,
) -> PersonStepHandOff | None:
    """The validated hand-off in a prepare answer; None when it is unusable.

    A command the test case does not state as a whole command
    (``_is_explicit_command``) is never handed out: the answer is then
    unusable and the caller falls back to the plan text. ``how_to`` is parsed
    leniently: bad items are dropped, never the hand-off. An item's source is
    kept only when it cites an id of ``allowed_items`` (``kb``) or a given
    profile (``profile``); an item that only restates the instruction or the
    expected result is dropped. Obvious secret values the case does not state
    are redacted everywhere except in a ``kind="command"`` step's command,
    which stays the case's exact text.
    """
    data = _json_object(raw)
    if data is None:
        return None
    kind = data.get("kind")
    instruction = data.get("instruction")
    expected = data.get("expected_result")
    command = data.get("command") or ""
    if not isinstance(instruction, str) or not instruction.strip():
        return None
    if not isinstance(expected, str) or not isinstance(command, str):
        return None
    if restore:
        kind = "restore"
        command = ""
    elif kind == "command":
        command = command.strip().strip(_QUOTES).strip()
        if not _is_explicit_command(command, task_description):
            logger.info(
                "Person step %d: command not stated by the test case, dropped",
                step_num,
            )
            return None
    elif kind == "interactive":
        command = ""
    else:
        return None
    instruction = cap_text(
        _redacted(strip_executor_vocabulary(instruction), task_description),
        INSTRUCTION_MAX_CHARS,
    )
    expected = cap_text(
        _redacted(strip_executor_vocabulary(expected), task_description),
        EXPECTED_RESULT_MAX_CHARS,
    )
    return PersonStepHandOff(
        step_num=step_num,
        kind="restore" if restore else ("command" if command else "interactive"),
        instruction=instruction,
        expected_result=expected,
        command=command,
        how_to=_parse_how_to(
            data.get("how_to"),
            step_num=step_num,
            context=_HowToContext(
                task_description=task_description,
                restated=(_comparable(instruction), _comparable(expected)),
                allowed_items=allowed_items or {},
                profile_given=profile_given,
            ),
        ),
    )


@dataclass(frozen=True)
class _HowToContext:
    """What a how-to item is validated against."""

    task_description: str
    # The instruction and the expected result, comparable (_comparable).
    restated: tuple[str, ...]
    # Knowledge base item ids the model may cite -> names.
    allowed_items: Mapping[int, str]
    profile_given: bool


def _parse_how_to(
    value: object, *, step_num: int, context: _HowToContext
) -> tuple[PersonHowToItem, ...]:
    """The usable "How to do it" items; suggested commands are not checked
    against the case text (only ``kind="command"`` is), only for shape."""
    if not isinstance(value, list):
        return ()
    items: list[PersonHowToItem] = []
    dropped = restated = 0
    for raw_item in value:
        item = _how_to_item(raw_item, context)
        if item is None:
            dropped += 1
            continue
        if (
            not item.command
            and not item.source_kind
            and _restates(item.text, context.restated)
        ):
            restated += 1
            continue
        if len(items) < HOW_TO_MAX_ITEMS:
            items.append(item)
    if dropped:
        logger.info("Person step %d: %d how-to item(s) dropped", step_num, dropped)
    if restated:
        logger.info(
            "Person step %d: %d how-to item(s) restating the step dropped",
            step_num,
            restated,
        )
    return tuple(items)


def _how_to_item(value: object, context: _HowToContext) -> PersonHowToItem | None:
    if not isinstance(value, dict):
        return None
    text = value.get("text")
    command = value.get("command") or ""
    if not isinstance(text, str) or not isinstance(command, str):
        return None
    text = cap_text(
        _redacted(strip_executor_vocabulary(text), context.task_description),
        HOW_TO_TEXT_MAX_CHARS,
    )
    command = _unwrap_command(command)
    if not text or len(command) > HOW_TO_COMMAND_MAX_CHARS:
        return None
    if "\n" in command or "\r" in command:
        return None
    command = _redacted(command, context.task_description)
    kind, item_id, page, label = _how_to_source(value.get("source"), context)
    return PersonHowToItem(
        text=text,
        command=command,
        source_kind=kind,
        source_item_id=item_id,
        source_page=page,
        source_label=label,
    )


def _how_to_source(
    value: object, context: _HowToContext
) -> tuple[PersonHowToSourceKind, int | None, int | None, str]:
    """A validated source (kind, item id, page, server-built label); an unknown
    or malformed source is dropped and the item kept."""
    if not isinstance(value, dict):
        return "", None, None, ""
    kind = value.get("kind")
    if kind == "profile" and context.profile_given:
        return "profile", None, None, PROFILE_SOURCE_LABEL
    if kind != "kb":
        return "", None, None, ""
    item_id = positive_int(value.get("item_id"))
    name = None if item_id is None else context.allowed_items.get(item_id)
    if item_id is None or name is None:
        return "", None, None, ""
    page = positive_int(value.get("page"))
    label = f"«{' '.join(name.split())}»" + ("" if page is None else f", page {page}")
    return "kb", item_id, page, cap_text(label, SOURCE_LABEL_MAX_CHARS)


def _redacted(text: str, task_description: str) -> str:
    return redact_secrets(text, known_text=task_description)


_NOT_WORD: Final = re.compile(r"[^\w]+")
# A how-to item contained in the instruction is dropped from this many words.
_RESTATED_MIN_WORDS: Final = 6


def _comparable(text: str) -> str:
    """Lower-case words only, single-spaced (for the restatement guard)."""
    return " ".join(_NOT_WORD.sub(" ", text.lower()).split())


def _restates(text: str, restated: tuple[str, ...]) -> bool:
    """Whether ``text`` says nothing beyond the instruction or expected result:
    equal to one of them, or a run of at least ``_RESTATED_MIN_WORDS`` words
    contained in one. Short items ("Open Settings") are kept. A cheap net
    under the prompt rule, not a similarity model."""
    item = _comparable(text)
    if not item:
        return False
    if item in restated:
        return True
    return len(item.split()) >= _RESTATED_MIN_WORDS and any(
        f" {item} " in f" {other} " for other in restated
    )


def _unwrap_command(command: str) -> str:
    """``command`` without wrapping backticks or quotes; a quote
    that belongs to the command (``"C:\\Program Files\\..." -version``) stays."""
    command = command.strip()
    while len(command) >= 2 and command[0] == command[-1] and command[0] in _QUOTES:
        inner = command[1:-1]
        if command[0] != "`" and command[0] in inner:
            break
        command = inner.strip()
    return command


def _is_explicit_command(command: str, task_description: str) -> bool:
    """Whether the case text states ``command`` as a whole command.

    A substring match is not enough: "rm -rf build" must not pass because the
    case says "rm -rf build && make". The command (whitespace collapsed) must
    equal one of the case's stated commands: a whole line (also a fenced code
    line), optionally after a list marker or a "$ "/"> " prompt, or a whole
    inline code span or quoted string.
    """
    if not command or "\n" in command or len(command) > COMMAND_MAX_CHARS:
        return False
    return _collapse(command) in _stated_commands(task_description)


def _stated_commands(task_description: str) -> set[str]:
    stated: set[str] = set()
    for line in task_description.splitlines():
        stripped = line.strip()
        stated.add(_collapse(stripped))
        stated.add(_collapse(_LINE_PREFIX.sub("", stripped)))
        for match in _QUOTED_SPAN.finditer(line):
            span = next(group for group in match.groups() if group is not None)
            stated.add(_collapse(span))
    stated.discard("")
    return stated


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _json_object(raw: str) -> dict[str, object] | None:
    match = _JSON_BLOCK.search(raw)
    text = match.group(1) if match else raw
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


@dataclass(frozen=True)
class PersonCheck:
    """The agent's check of a person step, with what it recorded as evidence."""

    result: SubTaskResult
    # Output of the read-only commands the agent ran (cleaned, uncapped).
    check_output: str


def confirm_person_step(
    person: CheckpointPersonStep,
    project_id: int,
    *,
    config: AgentConfig,
    browser_selection: BrowserSelection,
    system_info: dict[str, object] | None,
    project_prompt: str | None,
    state_description: str,
) -> PersonCheck:
    """Check a person's report of ``person.hand_off`` read-only.

    ``config`` is the step's config (screenshots tagged with the attempt); the
    budget becomes ``PERSON_STEP_CONFIRM_TIMEOUT_SECONDS`` with no final-answer
    turn. The attempt always has a screenshot: one is taken without the model
    when it took none. Faults (runner lost, AI service, ...) raise as for any
    sub-agent.
    """
    timeout_seconds: int = settings.PERSON_STEP_CONFIRM_TIMEOUT_SECONDS
    sub_task = build_confirm_sub_task(
        person, os_name=get_os_name(system_info), timeout_seconds=timeout_seconds
    )
    return _run_check(
        sub_task,
        project_id,
        config=replace(config, timeout_seconds=timeout_seconds, final_answer=None),
        browser_selection=browser_selection,
        system_info=system_info,
        project_prompt=project_prompt,
        state_description=state_description,
    )


def record_mark_evidence(
    person: CheckpointPersonStep,
    project_id: int,
    *,
    verdict: Literal["failed", "blocked"],
    config: AgentConfig,
    browser_selection: BrowserSelection,
    system_info: dict[str, object] | None,
    project_prompt: str | None,
    state_description: str,
) -> PersonCheck:
    """Evidence of a step a person marked failed or blocked; never a judgement.

    With ``PERSON_EVIDENCE_LLM`` the restricted sub-agent takes a screenshot
    and at most one read-only check; without, only a screenshot is taken (no
    model). Faults raise; the caller keeps the person's verdict.
    """
    if not settings.PERSON_EVIDENCE_LLM:
        if config.on_screenshot is not None:
            capture_screenshot(project_id, on_screenshot=config.on_screenshot)
        return PersonCheck(
            result=SubTaskResult(
                status="fail",
                summary="a screenshot was recorded (no check by the AI).",
                iterations=0,
            ),
            check_output="",
        )
    timeout_seconds: int = settings.PERSON_STEP_CONFIRM_TIMEOUT_SECONDS
    sub_task = build_mark_evidence_sub_task(
        person,
        verdict=verdict,
        os_name=get_os_name(system_info),
        timeout_seconds=timeout_seconds,
    )
    return _run_check(
        sub_task,
        project_id,
        config=replace(config, timeout_seconds=timeout_seconds, final_answer=None),
        browser_selection=browser_selection,
        system_info=system_info,
        project_prompt=project_prompt,
        state_description=state_description,
    )


def _run_check(
    sub_task: SubTask,
    project_id: int,
    *,
    config: AgentConfig,
    browser_selection: BrowserSelection,
    system_info: dict[str, object] | None,
    project_prompt: str | None,
    state_description: str,
) -> PersonCheck:
    """Run the restricted sub-agent; count its screenshots and keep the output
    of its commands (the ``$ `` log lines), then take a screenshot if it took
    none."""
    screenshots = 0
    outputs: list[str] = []
    on_screenshot = config.on_screenshot
    on_log = config.on_log

    def counted_screenshot(base64_data: str, tool_name: str) -> None:
        nonlocal screenshots
        screenshots += 1
        if on_screenshot is not None:
            on_screenshot(base64_data, tool_name)

    def kept_log(message: str) -> None:
        if message.startswith(COMMAND_LOG_PREFIX):
            outputs.append(message[len(COMMAND_LOG_PREFIX) :])
        if on_log is not None:
            on_log(message)

    result = run_sub_agent(
        sub_task,
        state_description,
        project_id,
        config=replace(
            config,
            on_screenshot=_counted(on_screenshot, counted_screenshot),
            on_log=kept_log,
        ),
        browser_selection=browser_selection,
        system_info=system_info,
        project_prompt=project_prompt,
        tool_names=CONFIRM_TOOL_NAMES,
    )
    if screenshots == 0 and on_screenshot is not None:
        logger.info("Person step check took no screenshot: taking one")
        if on_log is not None:
            on_log("[Evidence] The agent took no screenshot: taking one for the record")
        capture_screenshot(project_id, on_screenshot=on_screenshot)
    return PersonCheck(result=result, check_output="\n".join(outputs))


def _counted(
    on_screenshot: ScreenshotCallback | None, counted: ScreenshotCallback
) -> ScreenshotCallback | None:
    # No sink, no screenshots to count or to guarantee.
    return counted if on_screenshot is not None else None


def person_failure_result(
    summary: str,
    person: CheckpointPersonStep,
    check: PersonCheck,
    *,
    step_num: int,
    on_log: LogCallback | None,
) -> SubTaskResult:
    """A failed person attempt with its evidence as ``error`` (the pasted and
    check outputs, head and tail, fenced as data), logged once as
    ``[Evidence]``."""
    evidence = format_person_evidence(
        pasted_output=person.output,
        check_output=check.check_output,
        observation=check.result.summary,
        max_chars=settings.PERSON_EVIDENCE_OUTPUT_MAX_CHARS,
    )
    logger.info("Person step %d: evidence recorded", step_num)
    if on_log is not None:
        on_log(f"[Evidence] step {step_num}:\n{evidence}")
    return SubTaskResult(
        status="fail",
        summary=summary,
        iterations=check.result.iterations,
        error=evidence,
    )
