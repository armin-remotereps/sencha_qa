from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal, Protocol

from controller_client.protocol import DEFAULT_BROWSER, BrowserName


class ToolCategory(Enum):
    CONTROLLER = "controller"
    BROWSER = "browser"
    SEARCH = "search"
    UTILITY = "utility"
    # Read-only project data (test cases, application context) for the setup agent.
    PROJECT_DATA = "project_data"


class AgentStopReason(Enum):
    MAX_ITERATIONS = "max_iterations"
    TIMEOUT = "timeout"
    TASK_COMPLETE = "task_complete"
    ERROR = "error"
    CANCELLED = "cancelled"
    # The orchestrator paused the case at a failing step for a person (or the
    # setup agent) to fix the machine; ``AgentResult.help`` says why.
    NEEDS_HUMAN_HELP = "needs_human_help"


@dataclass(frozen=True)
class HelpRequestInfo:
    """Why a case stopped for help: the failing step and what to fix there."""

    # 1-based plan step the case stopped at; it re-runs after the fix.
    step_num: int
    reason: str
    # One or two imperative sentences: what to fix on the machine.
    ask: str


@dataclass(frozen=True)
class ToolParameter:
    name: str
    type: str
    description: str
    required: bool
    enum: tuple[str, ...] | None = None
    # For type="array": the JSON-schema type of the elements, and a size hint
    # (strict mode is off, so the handler enforces the cap).
    items_type: str | None = None
    max_items: int | None = None
    # Full JSON schema of the elements (e.g. objects); wins over ``items_type``.
    items_schema: dict[str, object] | None = None


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    category: ToolCategory
    parameters: tuple[ToolParameter, ...]


@dataclass(frozen=True)
class ToolCall:
    tool_call_id: str
    tool_name: str
    arguments: dict[str, object]


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    content: str
    is_error: bool


@dataclass(frozen=True)
class TextContent:
    text: str


@dataclass(frozen=True)
class ImageContent:
    base64_data: str
    media_type: str = "image/png"


ContentPart = TextContent | ImageContent

MessageDict = dict[str, object]
ToolSchema = dict[str, object]


@dataclass(frozen=True)
class ReasoningItem:
    """An opaque reasoning item returned by the Responses API.

    Replayed in front of the assistant turn it came with, so the model keeps its
    chain of thought across tool calls without OpenAI storing the conversation.
    """

    item_id: str
    encrypted_content: str
    summary: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChatMessage:
    role: str  # "system", "user", "assistant", "tool"
    content: str | tuple[ContentPart, ...] | None = None
    tool_calls: tuple[ToolCall, ...] | None = None
    tool_call_id: str | None = None
    # Set only on assistant messages parsed from a response; sent back verbatim.
    reasoning: tuple[ReasoningItem, ...] | None = None
    phase: str | None = None


@dataclass(frozen=True)
class LLMConfig:
    model: str
    api_key: str
    endpoint_url: str
    max_tokens: int
    # Responses API ``reasoning.summary`` ("auto", "concise", "detailed"); None omits it.
    reasoning_summary: str | None = None


@dataclass(frozen=True)
class LLMResponse:
    message: ChatMessage
    finish_reason: str
    usage_prompt_tokens: int
    usage_completion_tokens: int
    reasoning_content: str | None = None
    usage_reasoning_tokens: int = 0


LogCallback = Callable[[str], None]
CancellationCheck = Callable[[], bool]


class AgentCancelledError(Exception):
    pass


class ScreenshotCallback(Protocol):
    def __call__(self, base64_data: str, tool_name: str) -> None: ...


@dataclass(frozen=True)
class AgentConfig:
    llm: LLMConfig
    vision_llm: LLMConfig | None = None
    max_iterations: int = 300
    timeout_seconds: int = 900
    on_log: LogCallback | None = None
    on_screenshot: ScreenshotCallback | None = None
    cancellation_check: CancellationCheck | None = None


@dataclass(frozen=True)
class AgentResult:
    stop_reason: AgentStopReason
    iterations: int
    messages: tuple[ChatMessage, ...]
    error: str | None = None
    # Set only with ``AgentStopReason.NEEDS_HUMAN_HELP``.
    help: HelpRequestInfo | None = None


@dataclass(frozen=True)
class SubTask:
    description: str
    expected_result: str
    timeout_seconds: int


@dataclass(frozen=True)
class SubTaskResult:
    status: Literal["pass", "fail"]
    summary: str
    iterations: int
    error: str | None = None
    # Failed results only: what the attempt did (agents/attempt_digest.py), so
    # the recovery and the retried step do not start blind.
    actions_digest: str = ""


@dataclass(frozen=True)
class SubTaskAttempt:
    # 1-based planned step; a recovery carries the number of the step it recovers
    # and a resume (state restore before a resumed case) the step it prepares.
    step_num: int
    kind: Literal["step", "recovery", "resume"]
    result: SubTaskResult
    # Wall-clock seconds the sub-agent ran; None for checkpoints written before
    # durations were recorded.
    duration_seconds: int | None = None
    # Set on a synthetic passing ``step`` attempt written when a help chat
    # redesigned the plan: the earlier plan's step this one was carried from.
    carried_from: int | None = None


# What the orchestrator decides after a failed step.
DecisionAction = Literal["continue", "recover", "stop", "need_help"]

# The orchestrator's view of who is at fault for a failed step.
FailureCause = Literal["environment", "agent", "product", "unclear"]


@dataclass(frozen=True)
class OrchestratorDecision:
    action: DecisionAction
    reason: str
    recovery_task: SubTask | None = None
    # None when the model gave no (or an unknown) cause.
    cause: FailureCause | None = None
    # What a person should fix on the machine; set for need_help and recover.
    help_ask: str = ""


@dataclass(frozen=True)
class StepDecision:
    """Why the orchestrator continued, recovered, stopped or asked for help."""

    step_num: int
    action: DecisionAction
    reason: str


# What the orchestrator is doing at the cursor step: running it, judging its
# failure, running a recovery, restoring state before a resume, writing the
# verdict, or waiting for help at a failed step.
OrchestratorPhase = Literal[
    "step", "evaluating", "recovery", "resume", "verdict", "awaiting_help"
]


@dataclass(frozen=True)
class OrchestratorResult:
    status: Literal["pass", "fail", "need_help"]
    summary: str
    attempts: tuple[SubTaskAttempt, ...]
    total_iterations: int
    error: str | None = None
    # Set only when ``status == "need_help"``.
    help: HelpRequestInfo | None = None


# Who fixed the machine after a case asked for help.
HelpSource = Literal["human", "auto_fix"]


@dataclass(frozen=True)
class CheckpointHelp:
    """The last help a case got, stored in its checkpoint.

    Written by ``projects`` when a person continues the case or the setup
    agent reports the machine fixed; read by the orchestrator on resume.
    """

    # 1-based step the case stopped at and re-runs.
    step_num: int
    # ``projects.CaseHelpRequest`` id; None when unknown.
    request_id: int | None
    # The person's note; empty when the setup agent continued the case.
    note: str
    # ``human`` when a person continued the case (after an auto-fix that did not
    # fix it, when ``auto_fix_summary`` is set), ``auto_fix`` when the setup
    # agent reported the machine fixed.
    source: HelpSource
    # True until the resumed case writes its first checkpoint: the pre-case
    # cleanup and the restore sub-task are skipped while it is set.
    continue_pending: bool
    # What the agent asked to fix; may be empty (older checkpoints).
    ask: str = ""
    # The setup agent's report when an auto-fix ran for this request; may be empty.
    auto_fix_summary: str = ""
    # That auto-fix reported the machine fixed (with ``source`` ``human`` when a
    # person continued during it, or after its fix failed the run's gates).
    auto_fix_fixed: bool = False
    # Label of the infrastructure fault that interrupted the step; empty when
    # the agent asked for help (and on older checkpoints).
    fault: str = ""


@dataclass(frozen=True)
class PlanRevisionRecord:
    """An earlier plan replaced by a help-chat redesign (display and prompt data).

    ``attempts``/``decisions`` of the checkpoint describe the current plan only;
    the earlier plan's moved here when the revision was applied.
    """

    # ``plan_version`` the checkpoint got when this revision was applied.
    version: int
    # ``projects.CasePlanRevision`` / ``CaseHelpRequest`` ids.
    revision_id: int
    help_request_id: int
    reason: str
    # (description, expected_result) of every earlier step.
    previous_plan: tuple[tuple[str, str], ...]
    # The earlier plan's attempts, without actions digests.
    earlier_attempts: tuple[SubTaskAttempt, ...]
    earlier_decisions: tuple[StepDecision, ...]
    # (new step, earlier step) for every carried step.
    mapping: tuple[tuple[int, int], ...]
    # The earlier step the case paused at, and its failed attempt's digest.
    paused_step: int
    paused_digest: str
    # First new step that was not carried (where the case resumed).
    resume_step: int


@dataclass(frozen=True)
class OrchestratorCheckpoint:
    """Progress of one case, persisted so a blocked case can resume in place.

    ``next_step`` is the 1-based step that is running or runs next.
    """

    plan: tuple[SubTask, ...]
    attempts: tuple[SubTaskAttempt, ...]
    next_step: int
    browser: BrowserName
    total_iterations: int
    # Display-only progress detail for the case page and TestRail comments.
    decisions: tuple[StepDecision, ...] = ()
    phase: OrchestratorPhase = "step"
    phase_started_at: datetime | None = None
    help: CheckpointHelp | None = None
    # Number of help-chat redesigns applied, and the newest earlier plans.
    plan_version: int = 0
    plan_revisions: tuple[PlanRevisionRecord, ...] = ()


CheckpointCallback = Callable[[OrchestratorCheckpoint], None]

# Called after every message the tool loop appends, with that message and the
# full current transcript (append-only).
TranscriptCallback = Callable[[ChatMessage, tuple[ChatMessage, ...]], None]


@dataclass
class BrowserSelection:
    """The browser a case drives; mutable on purpose.

    One instance per test case, shared by every sub-agent's ``ToolContext`` so
    a browser picked on the first navigate stays selected for the whole case.
    """

    current: BrowserName = DEFAULT_BROWSER


@dataclass(frozen=True)
class ToolContext:
    project_id: int
    browser_selection: BrowserSelection
    summarizer_config: LLMConfig | None = None
    vision_config: LLMConfig | None = None
    on_screenshot: ScreenshotCallback | None = None
    on_log: LogCallback | None = None
    cancellation_check: CancellationCheck | None = None
    deadline: float | None = None


ToolDispatcher = Callable[[ToolCall, ToolContext], ToolResult]


@dataclass(frozen=True)
class PixelBBox:
    x_min: int
    y_min: int
    x_max: int
    y_max: int


@dataclass(frozen=True)
class PixelUIElement:
    index: int
    type: str
    content: str
    bbox: PixelBBox
    center_x: int
    center_y: int
    interactivity: bool


@dataclass(frozen=True)
class PixelParseResult:
    annotated_image: str
    elements: tuple[PixelUIElement, ...]
    image_width: int
    image_height: int


@dataclass(frozen=True)
class Verdict:
    accepted: bool
    reason: str


OutcomeStatus = Literal["confirmed", "unexpected", "missed"]


@dataclass(frozen=True)
class OutcomeVerdict:
    """Outcome check of an action: ``unexpected`` means the intended element was
    acted on and the screen changed, but not as expected; ``missed`` means nothing
    changed or a different element was acted on."""

    status: OutcomeStatus
    reason: str


@dataclass(frozen=True)
class RejectedCandidate:
    attempt: int
    index: int
    center_x: int
    center_y: int
    content: str
    stage: Literal["candidate", "outcome"]
    reason: str
