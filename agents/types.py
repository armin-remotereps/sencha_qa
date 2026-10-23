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


@dataclass(frozen=True)
class OrchestratorDecision:
    action: Literal["continue", "recover", "stop"]
    reason: str
    recovery_task: SubTask | None = None


@dataclass(frozen=True)
class StepDecision:
    """Why the orchestrator continued, recovered or stopped after a failed step."""

    step_num: int
    action: Literal["continue", "recover", "stop"]
    reason: str


# What the orchestrator is doing at the cursor step: running it, judging its
# failure, running a recovery, restoring state before a resume, or writing the
# verdict.
OrchestratorPhase = Literal["step", "evaluating", "recovery", "resume", "verdict"]


@dataclass(frozen=True)
class OrchestratorResult:
    status: Literal["pass", "fail"]
    summary: str
    attempts: tuple[SubTaskAttempt, ...]
    total_iterations: int
    error: str | None = None


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


@dataclass(frozen=True)
class RejectedCandidate:
    attempt: int
    index: int
    center_x: int
    center_y: int
    content: str
    stage: Literal["candidate", "outcome"]
    reason: str
