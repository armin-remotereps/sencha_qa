from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agents.services.playwright_session import PlaywrightSessionManager
    from agents.services.ssh_session import SSHSessionManager
    from agents.services.vnc_session import VncSessionManager
    from environments.types import ContainerPorts


class ToolCategory(Enum):
    SHELL = "shell"
    SCREEN = "screen"
    BROWSER = "browser"
    VNC = "vnc"


class AgentStopReason(Enum):
    MAX_ITERATIONS = "max_iterations"
    TIMEOUT = "timeout"
    TASK_COMPLETE = "task_complete"
    ERROR = "error"


@dataclass(frozen=True)
class ToolParameter:
    name: str
    type: str
    description: str
    required: bool
    enum: tuple[str, ...] | None = None


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


# Multimodal content
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
class ChatMessage:
    role: str  # "system", "user", "assistant", "tool"
    content: str | tuple[ContentPart, ...] | None = None
    tool_calls: tuple[ToolCall, ...] | None = None
    tool_call_id: str | None = None


@dataclass(frozen=True)
class DMRConfig:
    host: str
    port: str
    model: str
    temperature: float = 0.9
    max_tokens: int = 4096


@dataclass(frozen=True)
class DMRResponse:
    message: ChatMessage
    finish_reason: str
    usage_prompt_tokens: int
    usage_completion_tokens: int
    reasoning_content: str | None = None


@dataclass(frozen=True)
class AgentConfig:
    dmr: DMRConfig
    vision_dmr: DMRConfig | None = None
    max_iterations: int = 30
    timeout_seconds: int = 900


@dataclass(frozen=True)
class AgentResult:
    stop_reason: AgentStopReason
    iterations: int
    messages: tuple[ChatMessage, ...]
    error: str | None = None


@dataclass(frozen=True)
class ToolContext:
    ports: ContainerPorts
    ssh_session: SSHSessionManager
    playwright_session: PlaywrightSessionManager
    vnc_session: VncSessionManager
    summarizer_config: DMRConfig | None = None
    vision_config: DMRConfig | None = None
