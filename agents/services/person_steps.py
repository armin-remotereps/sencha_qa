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
from dataclasses import replace
from typing import Final

from django.conf import settings

from agents.os_shell import get_os_name
from agents.services.llm_client import send_chat_completion
from agents.services.person_step_prompts import (
    COMMAND_MAX_CHARS,
    EXPECTED_RESULT_MAX_CHARS,
    HOW_TO_COMMAND_MAX_CHARS,
    HOW_TO_MAX_ITEMS,
    HOW_TO_TEXT_MAX_CHARS,
    INSTRUCTION_MAX_CHARS,
    build_confirm_sub_task,
    build_prepare_system_prompt,
    build_prepare_user_prompt,
    build_restore_user_prompt,
    cap_text,
    fallback_hand_off,
    strip_executor_vocabulary,
)
from agents.services.sub_agent import run_sub_agent
from agents.types import (
    AgentConfig,
    BrowserSelection,
    ChatMessage,
    CheckpointPersonStep,
    LLMConfig,
    PersonHowToItem,
    PersonStepHandOff,
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
) -> PersonStepHandOff:
    """The hand-off of ``sub_task`` (or, with ``restore``, of the restore task).

    An unusable answer falls back to the step's own text as an interactive
    step; an ``AIServiceError`` (the provider failed) propagates as a fault.
    """
    os_name = get_os_name(system_info)
    user_prompt = (
        build_restore_user_prompt(sub_task)
        if restore
        else build_prepare_user_prompt(
            sub_task,
            task_description=task_description,
            step_num=step_num,
            total_steps=total_steps,
        )
    )
    response = send_chat_completion(
        llm,
        (
            ChatMessage(
                role="system",
                content=build_prepare_system_prompt(os_name=os_name, restore=restore),
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
    raw: str, *, step_num: int, task_description: str, restore: bool
) -> PersonStepHandOff | None:
    """The validated hand-off in a prepare answer; None when it is unusable.

    A command the test case does not state as a whole command
    (``_is_explicit_command``) is never handed out: the answer is then
    unusable and the caller falls back to the plan text. ``how_to`` is parsed
    leniently: bad items are dropped, never the hand-off.
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
    return PersonStepHandOff(
        step_num=step_num,
        kind="restore" if restore else ("command" if command else "interactive"),
        instruction=cap_text(
            strip_executor_vocabulary(instruction), INSTRUCTION_MAX_CHARS
        ),
        expected_result=cap_text(
            strip_executor_vocabulary(expected), EXPECTED_RESULT_MAX_CHARS
        ),
        command=command,
        how_to=_parse_how_to(data.get("how_to"), step_num=step_num),
    )


def _parse_how_to(value: object, *, step_num: int) -> tuple[PersonHowToItem, ...]:
    """The usable "How to do it" items; suggested commands are not checked
    against the case text (only ``kind="command"`` is), only for shape."""
    if not isinstance(value, list):
        return ()
    items: list[PersonHowToItem] = []
    dropped = 0
    for raw_item in value:
        item = _how_to_item(raw_item)
        if item is None:
            dropped += 1
            continue
        if len(items) < HOW_TO_MAX_ITEMS:
            items.append(item)
    if dropped:
        logger.info("Person step %d: %d how-to item(s) dropped", step_num, dropped)
    return tuple(items)


def _how_to_item(value: object) -> PersonHowToItem | None:
    if not isinstance(value, dict):
        return None
    text = value.get("text")
    command = value.get("command") or ""
    if not isinstance(text, str) or not isinstance(command, str):
        return None
    text = cap_text(strip_executor_vocabulary(text), HOW_TO_TEXT_MAX_CHARS)
    command = _unwrap_command(command)
    if not text or len(command) > HOW_TO_COMMAND_MAX_CHARS:
        return None
    if "\n" in command or "\r" in command:
        return None
    return PersonHowToItem(text=text, command=command)


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


def confirm_person_step(
    person: CheckpointPersonStep,
    project_id: int,
    *,
    config: AgentConfig,
    browser_selection: BrowserSelection,
    system_info: dict[str, object] | None,
    project_prompt: str | None,
    state_description: str,
) -> SubTaskResult:
    """Check a person's report of ``person.hand_off`` read-only.

    ``config`` is the step's config (screenshots tagged with the attempt); the
    budget becomes ``PERSON_STEP_CONFIRM_TIMEOUT_SECONDS`` with no final-answer
    turn. Faults (runner lost, AI service, ...) raise as for any sub-agent.
    """
    timeout_seconds: int = settings.PERSON_STEP_CONFIRM_TIMEOUT_SECONDS
    sub_task = build_confirm_sub_task(
        person, os_name=get_os_name(system_info), timeout_seconds=timeout_seconds
    )
    return run_sub_agent(
        sub_task,
        state_description,
        project_id,
        config=replace(config, timeout_seconds=timeout_seconds, final_answer=None),
        browser_selection=browser_selection,
        system_info=system_info,
        project_prompt=project_prompt,
        tool_names=CONFIRM_TOOL_NAMES,
    )
