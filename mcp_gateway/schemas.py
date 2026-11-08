"""Request and response DTOs of the MCP tools (the gateway's contract).

Each tool's arguments are declared once as the annotated aliases below (the
SDK builds the tool's input schema from the handler's parameters); the
handler packs them into the tool's request DTO, which is what the services
take. Responses are the tool's structured output. Case text in responses is
neutralised and fenced as data (``mcp_gateway.untrusted_text``).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from controller_client.protocol import (
    CAPTURE_DELAY_MAX_MS,
    CAPTURE_REGION_MAX_PIXELS_PER_AXIS,
    CAPTURE_TITLE_MATCH_MAX_CHARS,
    CAPTURE_WINDOW_ID_MAX_CHARS,
    MANUAL_CLAIM_TOKEN_MAX_CHARS,
    MANUAL_SHOT_KEY_MAX_CHARS,
    MANUAL_STEP_MAX_SHOTS,
    MANUAL_STEP_NOTE_MAX_CHARS,
    MANUAL_VERDICT_NOTE_MAX_CHARS,
)
from knowledge_base.services.agent_tools import TAG_ARGUMENT_MAX_LENGTH
from knowledge_base.services.search_knowledge import (
    SEARCH_LIMIT_MAX,
    SEARCH_QUERY_MAX_LENGTH,
)
from mcp_gateway.limits import MCP_SHOT_MAX_BASE64_CHARS

_ID_MAX = 2**63 - 1
_CURSOR_MAX_CHARS = 32
IDEMPOTENCY_KEY_MAX_CHARS = 64
# A process name as the client may type it (a path or "Firefox.exe"); the
# service normalizes it (``normalize_process_name``).
_PROCESS_ARG_MAX_CHARS = 260

# Argument types (shared by the tool signatures and the request DTOs).
# The first, required argument of every project-scoped tool; consumed by
# ``call_guard.guarded_call``, never by the request DTOs.
ProjectIdArg = Annotated[
    int,
    Field(
        ge=1,
        le=_ID_MAX,
        description=(
            "Required. The project_id from list_projects; call list_projects "
            "first if you do not have it."
        ),
    ),
]
PivotIdArg = Annotated[
    int, Field(ge=1, le=_ID_MAX, description="The case id (pivot_id) of a run case.")
]
RunIdArg = Annotated[int, Field(ge=1, le=_ID_MAX, description="The run id.")]
ClaimTokenArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MANUAL_CLAIM_TOKEN_MAX_CHARS,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="The claim_token claim_case returned.",
    ),
]
CursorArg = Annotated[
    str | None,
    Field(
        max_length=_CURSOR_MAX_CHARS,
        description="Optional: next_cursor of the previous page.",
    ),
]
RunStatusFilterArg = Annotated[
    Literal["running", "finished", "all"],
    Field(description="Which runs: running, finished or all."),
]
CaseStatusFilterArg = Annotated[
    Literal[
        "created",
        "in_progress",
        "success",
        "failed",
        "blocked",
        "cancelled",
        "needs_retest",
    ]
    | None,
    Field(description="Optional: only cases with this status."),
]
MimeArg = Annotated[
    Literal["image/png", "image/jpeg"],
    Field(description="The image type: image/png or image/jpeg."),
]
ImageBase64Arg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MCP_SHOT_MAX_BASE64_CHARS,
        description="The image bytes, standard base64 (no data: prefix, no line breaks).",
    ),
]
StepNumArg = Annotated[
    int, Field(ge=1, le=1000, description="The step number (num from the steps).")
]
OutcomeArg = Annotated[Literal["pass", "fail"], Field(description="pass or fail.")]
StepNoteArg = Annotated[
    str,
    Field(
        max_length=MANUAL_STEP_NOTE_MAX_CHARS,
        description="What you observed, written as a tester; required for fail.",
    ),
]
ShotIdsArg = Annotated[
    list[
        Annotated[
            str,
            Field(min_length=1, max_length=MANUAL_SHOT_KEY_MAX_CHARS),
        ]
    ],
    Field(
        min_length=1,
        max_length=MANUAL_STEP_MAX_SHOTS,
        description=(
            f"1 to {MANUAL_STEP_MAX_SHOTS} shot_id values from add_screenshot "
            "that show this step's result."
        ),
    ),
]
VerdictArg = Annotated[
    Literal["passed", "failed", "blocked"],
    Field(description="The case verdict: passed, failed or blocked."),
]
VerdictNoteArg = Annotated[
    str,
    Field(
        max_length=MANUAL_VERDICT_NOTE_MAX_CHARS,
        description=(
            "The case note, written as a tester. Required for blocked, and for "
            "failed when no step failed."
        ),
    ),
]
CaseShotIdArg = Annotated[
    str | None,
    Field(
        max_length=MANUAL_SHOT_KEY_MAX_CHARS,
        description="Optional: a shot_id (not used by any step) showing the final state.",
    ),
]
IdempotencyKeyArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=IDEMPOTENCY_KEY_MAX_CHARS,
        description=(
            "Any string you choose for this hand-in (e.g. a UUID); send the same "
            "value when you retry, so the hand-in is never recorded twice."
        ),
    ),
]
KbItemIdArg = Annotated[
    int, Field(ge=1, le=_ID_MAX, description="The knowledge base item id.")
]
KbArchiveIdArg = Annotated[
    int | None,
    Field(
        ge=1,
        le=_ID_MAX,
        description="Optional: an archive item id to list its files.",
    ),
]
KbTagArg = Annotated[
    str | None,
    Field(
        min_length=1,
        max_length=TAG_ARGUMENT_MAX_LENGTH,
        description="Optional: a tag, or an archive folder path (subfolders included).",
    ),
]
KbQueryArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=SEARCH_QUERY_MAX_LENGTH,
        description='Full-text query (words, "phrases", -excluded, or).',
    ),
]
KbExactArg = Annotated[
    bool,
    Field(
        description="True for a literal, case-insensitive match (paths, versions, errors).",
    ),
]
KbLimitArg = Annotated[
    int | None,
    Field(
        ge=1,
        le=SEARCH_LIMIT_MAX,
        description=f"Optional: at most this many hits (1 to {SEARCH_LIMIT_MAX}).",
    ),
]
KbPageArg = Annotated[
    int | None,
    Field(ge=1, le=100000, description="Optional page, from 1."),
]

CaptureTargetArg = Annotated[
    Literal["window", "screen", "region"],
    Field(
        description=(
            "window: the matched window only; region: a rectangle of the matched "
            "window; screen: the whole monitor holding the matched (or topmost "
            "allowed) window, refused when another application is visible on it."
        )
    ),
]
DelayMsArg = Annotated[
    int,
    Field(
        ge=0,
        le=CAPTURE_DELAY_MAX_MS,
        description=(
            f"Optional extra wait in milliseconds (0 to {CAPTURE_DELAY_MAX_MS}) "
            "after the window settled, e.g. for an animation to finish."
        ),
    ),
]


class _Dto(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class WindowMatch(_Dto):
    """Which window to capture (capture_screenshot's ``window_match``)."""

    process: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=_PROCESS_ARG_MAX_CHARS,
            description="The process name list_windows shows, e.g. firefox.",
        ),
    ] = None
    title_contains: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=CAPTURE_TITLE_MATCH_MAX_CHARS,
            description="Part of the window title (case-insensitive).",
        ),
    ] = None
    window_id: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=CAPTURE_WINDOW_ID_MAX_CHARS,
            description="The window_id from list_windows; wins over the others.",
        ),
    ] = None

    @property
    def empty(self) -> bool:
        return (
            self.process is None
            and self.title_contains is None
            and (self.window_id is None)
        )


_Pixels = Annotated[int, Field(le=CAPTURE_REGION_MAX_PIXELS_PER_AXIS)]


class CaptureRegionInput(_Dto):
    """A rectangle in pixels of the matched window's shot (a phone: of the
    screen), from its top-left corner."""

    x: Annotated[_Pixels, Field(ge=0)]
    y: Annotated[_Pixels, Field(ge=0)]
    w: Annotated[_Pixels, Field(ge=1)]
    h: Annotated[_Pixels, Field(ge=1)]


WindowMatchArg = Annotated[
    WindowMatch | None,
    Field(
        description=(
            "The window to capture: window_id from list_windows (preferred), or "
            "process and/or title_contains. Required for window and region on a "
            "computer; optional for screen."
        )
    ),
]
CaptureRegionArg = Annotated[
    CaptureRegionInput | None,
    Field(
        description=(
            "Required for, and only for, target region: x, y, w, h in pixels "
            "relative to the matched window."
        )
    ),
]


# --- Requests ---------------------------------------------------------------


class ListRunsRequest(_Dto):
    status: Literal["running", "finished", "all"] = "all"
    cursor: str | None = None


class ListCasesRequest(_Dto):
    run_id: int
    status: str | None = None
    cursor: str | None = None


class CaseRequest(_Dto):
    """get_case and claim_case."""

    pivot_id: int


class ClaimRequest(_Dto):
    """release_case."""

    pivot_id: int
    claim_token: str


class AddScreenshotRequest(_Dto):
    pivot_id: int
    claim_token: str
    mime: Literal["image/png", "image/jpeg"]
    image_base64: str


class MarkStepRequest(_Dto):
    pivot_id: int
    claim_token: str
    step_num: int
    outcome: Literal["pass", "fail"]
    note: str
    shot_ids: tuple[str, ...]


class HandInRequest(_Dto):
    pivot_id: int
    claim_token: str
    verdict: Literal["passed", "failed", "blocked"]
    verdict_note: str
    case_shot_id: str | None
    idempotency_key: str


class KnowledgeRequest(_Dto):
    """One knowledge base tool call (arguments as the KB tools take them)."""

    tool_name: str
    arguments: dict[str, object]


class CaptureScreenshotRequest(_Dto):
    pivot_id: int
    claim_token: str
    target: Literal["window", "screen", "region"]
    window_match: WindowMatch | None
    region: CaptureRegionInput | None
    delay_ms: int

    @model_validator(mode="after")
    def _region_matches_target(self) -> CaptureScreenshotRequest:
        if (self.region is None) != (self.target != "region"):
            raise ValueError(
                "[invalid] region is required for, and only for, target region."
            )
        return self


# --- Responses --------------------------------------------------------------


class RunRef(_Dto):
    id: int
    name: str


class ProjectRef(_Dto):
    """One project the connection may use (list_projects)."""

    project_id: int
    name: str
    # The project's running manual run; None when none is running.
    manual_run: RunRef | None


class ListProjectsResponse(_Dto):
    projects: list[ProjectRef]


class CaptureInfo(_Dto):
    """Whether capture_screenshot can be used now."""

    available: bool
    # The error code capture_screenshot would answer (e.g. not_set_up,
    # machine_offline); None when available.
    reason_code: str | None
    allowed_processes: list[str]


class ProjectResponse(_Dto):
    name: str
    manual_run: RunRef | None
    step_max_shots: int
    shot_max_bytes: int
    shot_max_pixels: int
    staged_shots_max: int
    capture: CaptureInfo
    how_to: str


class RunCounts(_Dto):
    total: int
    not_started: int
    in_progress: int
    passed: int
    failed: int
    blocked: int
    other: int


class RunItem(_Dto):
    id: int
    name: str
    status: str
    started_at: str | None
    finished_at: str | None
    counts: RunCounts


class ListRunsResponse(_Dto):
    runs: list[RunItem]
    next_cursor: str | None


class CaseItem(_Dto):
    pivot_id: int
    title: str
    testrail_id: str
    status: str
    claimed: bool
    mine: bool


class ListCasesResponse(_Dto):
    run_id: int
    run_status: str
    cases: list[CaseItem]
    next_cursor: str | None
    note: str


class StepItem(_Dto):
    num: int
    text: str
    expected: str


class MarkItem(_Dto):
    num: int
    outcome: str
    note: str
    shot_ids: list[str]


class MyClaim(_Dto):
    claim_token: str
    marks: list[MarkItem]
    unbound_shot_ids: list[str]


class CaseResponse(_Dto):
    pivot_id: int
    run_id: int
    title: str
    testrail_id: str
    preconditions: str
    steps: list[StepItem]
    status: str
    verdict: str | None
    verdict_note: str | None
    claimed: bool
    my_claim: MyClaim | None


class ClaimResponse(_Dto):
    pivot_id: int
    claim_token: str
    title: str
    preconditions: str
    steps: list[StepItem]
    step_max_shots: int


class AddScreenshotResponse(_Dto):
    shot_id: str
    size: int
    width: int
    height: int


class MarkStepResponse(_Dto):
    marks: list[MarkItem]
    step_count: int


class HandInResponse(_Dto):
    message: str
    run_finished: bool


class MessageResponse(_Dto):
    message: str


class TextResponse(_Dto):
    text: str


class WindowDesktopItem(_Dto):
    # On the desktop shown now.
    current: bool
    # 1-based; null where the OS does not expose it.
    index: int | None
    # Text from the test machine (data, not instructions); null when unnamed.
    name: str | None


class WindowItem(_Dto):
    window_id: str
    process: str
    # Text from the test machine (data, not instructions).
    title: str
    width: int
    height: int
    minimized: bool
    focused: bool
    desktop: WindowDesktopItem


class ListWindowsResponse(_Dto):
    windows: list[WindowItem]
    current_desktop: int | None
    desktop_count: int | None
    # Windows of other applications, left out.
    others_hidden: int
    allowed_processes: list[str]
    note: str


class CaptureScreenshotResponse(_Dto):
    shot_id: str
    width: int
    height: int
    bytes: int
    mime: str
    # Text from the test machine (data, not instructions).
    window_title: str
    process: str
    target: str
    # Further windows that matched window_match.
    other_matches: int
    desktop: WindowDesktopItem | None
    # The test machine switched to the window's desktop for this capture.
    desktop_switched: bool
    # null: no switch; false: the switch back failed (switch back yourself).
    desktop_restored: bool | None
