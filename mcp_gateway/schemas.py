"""Request and response DTOs of the MCP tools (the gateway's contract).

Each tool's arguments are declared once as the annotated aliases below (the
SDK builds the tool's input schema from the handler's parameters); the
handler packs them into the tool's request DTO, which is what the services
take. Responses are the tool's structured output. Case text in responses is
neutralised and fenced as data (``mcp_gateway.untrusted_text``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema, model_validator

from controller_client.protocol import (
    CAPTURE_DELAY_MAX_MS,
    CAPTURE_REGION_MAX_PIXELS_PER_AXIS,
    CAPTURE_TITLE_MATCH_MAX_CHARS,
    CAPTURE_WINDOW_ID_MAX_CHARS,
    DEFAULT_DRAG_SECONDS,
    DRAG_MAX_SECONDS,
    DRAG_MIN_SECONDS,
    INTERACTIVE_SESSION_ID_MAX_CHARS,
    LONG_PRESS_MAX_SECONDS,
    LONG_PRESS_MIN_SECONDS,
    MANUAL_CLAIM_TOKEN_MAX_CHARS,
    MANUAL_SHOT_KEY_MAX_CHARS,
    MANUAL_STEP_MAX_SHOTS,
    MANUAL_STEP_NOTE_MAX_CHARS,
    MANUAL_VERDICT_NOTE_MAX_CHARS,
    MAX_SCROLL_AMOUNT,
    SWIPE_MAX_DISTANCE,
    SWIPE_MAX_DURATION,
    SWIPE_MIN_DISTANCE,
    SWIPE_MIN_DURATION,
    WEB_ATTRIBUTE_NAME_MAX_CHARS,
    WEB_KEY_MAX_CHARS,
    WEB_SELECT_VALUE_MAX_CHARS,
    WEB_SELECTOR_MAX_CHARS,
    WEB_TAB_INDEX_MAX,
    WEB_TIMEOUT_MS_DEFAULT,
    WEB_TYPE_TEXT_MAX_CHARS,
    BrowserName,
    DeviceOrientation,
    MobileButton,
    ScrollDirection,
    WebBrowser,
    WebHistoryAction,
    WebReadWhat,
    WebTabsAction,
    WebWaitState,
)
from knowledge_base.services.agent_tools import TAG_ARGUMENT_MAX_LENGTH
from knowledge_base.services.search_knowledge import (
    SEARCH_LIMIT_MAX,
    SEARCH_QUERY_MAX_LENGTH,
)
from mcp_gateway.limits import (
    MCP_BROWSE_ARGS_MAX_CHARS,
    MCP_BROWSE_EVAL_TIMEOUT_MS_MAX,
    MCP_BROWSE_INDEX_MAX,
    MCP_BROWSE_SCRIPT_MAX_CHARS,
    MCP_READ_OUTPUT_MAX_WAIT_SECONDS,
    MCP_SHOT_MAX_BASE64_CHARS,
    MCP_WAIT_MAX_SECONDS,
)

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

# --- Machine tool arguments (docs/plan/feat-mcp-runner-tools.md) ---
_COMMAND_MAX_CHARS = 10_000
_PATH_MAX_CHARS = 1024
_DESCRIPTION_MAX_CHARS = 1000
_TYPED_TEXT_MAX_CHARS = 10_000
_KEYS_MAX_CHARS = 100
_APP_NAME_MAX_CHARS = 260
_URL_MAX_CHARS = 2048
_SEARCH_QUERY_MAX_CHARS = 500
_MachinePixel = Annotated[int, Field(ge=0, le=CAPTURE_REGION_MAX_PIXELS_PER_AXIS)]

CommandArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=_COMMAND_MAX_CHARS,
        description=(
            "The shell command to run on the test machine (cmd.exe on Windows, "
            "the login shell elsewhere; on a phone project, on the computer the "
            "phone is attached to)."
        ),
    ),
]
CwdArg = Annotated[
    str | None,
    Field(
        min_length=1,
        max_length=_PATH_MAX_CHARS,
        description="Optional working directory (absolute path).",
    ),
]
DetachArg = Annotated[
    bool,
    Field(
        description=(
            "True: start the command and return its session_id at once; read its "
            "output later with read_command_output. False (default): wait up to "
            "about 40 seconds, then return the output so far and, if it still "
            "runs, its session_id."
        )
    ),
]
SessionIdArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=INTERACTIVE_SESSION_ID_MAX_CHARS,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="The session_id execute_command or list_commands returned.",
    ),
]
WaitSecondsArg = Annotated[
    int,
    Field(
        ge=0,
        le=MCP_READ_OUTPUT_MAX_WAIT_SECONDS,
        description=(
            "Optional: wait up to this many seconds (0 to "
            f"{MCP_READ_OUTPUT_MAX_WAIT_SECONDS}) for the command to end; it "
            "returns earlier when it ends."
        ),
    ),
]
InputTextArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=_TYPED_TEXT_MAX_CHARS,
        description=(
            "Text to send to the command's input; end it with a newline to "
            "press Enter."
        ),
    ),
]
DescriptionArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=_DESCRIPTION_MAX_CHARS,
        description=(
            "The element in plain words, as it looks on screen (e.g. 'the Save "
            "button in the toolbar')."
        ),
    ),
]
ExpectedResultArg = Annotated[
    str | None,
    Field(
        min_length=1,
        max_length=_DESCRIPTION_MAX_CHARS,
        description=(
            "Optional: what the screen should show afterwards; the action is "
            "checked against it."
        ),
    ),
]
StartDescriptionArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=_DESCRIPTION_MAX_CHARS,
        description="The element to drag from, in plain words.",
    ),
]
EndDescriptionArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=_DESCRIPTION_MAX_CHARS,
        description="The element to drag to, in plain words.",
    ),
]
XArg = Annotated[
    _MachinePixel,
    Field(description="Horizontal pixel in take_screenshot's width x height."),
]
YArg = Annotated[
    _MachinePixel,
    Field(description="Vertical pixel in take_screenshot's width x height."),
]
StartXArg = Annotated[
    _MachinePixel,
    Field(
        description=(
            "Horizontal pixel where the drag starts, in take_screenshot's "
            "width x height."
        )
    ),
]
StartYArg = Annotated[
    _MachinePixel,
    Field(
        description=(
            "Vertical pixel where the drag starts, in take_screenshot's "
            "width x height."
        )
    ),
]
EndXArg = Annotated[
    _MachinePixel,
    Field(
        description=(
            "Horizontal pixel where the drag ends, in take_screenshot's "
            "width x height."
        )
    ),
]
EndYArg = Annotated[
    _MachinePixel,
    Field(
        description=(
            "Vertical pixel where the drag ends, in take_screenshot's "
            "width x height."
        )
    ),
]
OptionalXArg = Annotated[
    _MachinePixel | None,
    Field(description="Optional horizontal pixel (give it together with y)."),
]
OptionalYArg = Annotated[
    _MachinePixel | None,
    Field(description="Optional vertical pixel (give it together with x)."),
]
TypedTextArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=_TYPED_TEXT_MAX_CHARS,
        description="The text to type into the focused field.",
    ),
]
KeysArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=_KEYS_MAX_CHARS,
        description="A key or key combination, e.g. 'Return', 'ctrl+c', 'alt+F4'.",
    ),
]
ScrollDirectionArg = Annotated[
    ScrollDirection,
    Field(
        description=(
            "scroll: the content direction (down reveals content below). swipe: "
            "the direction the finger moves."
        )
    ),
]
ScrollAmountArg = Annotated[
    int | None,
    Field(
        ge=1,
        le=MAX_SCROLL_AMOUNT,
        description=f"Optional: how far, 1 to {MAX_SCROLL_AMOUNT}.",
    ),
]
AppNameArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=_APP_NAME_MAX_CHARS,
        description=(
            "The application: its name (desktop), or its package name, bundle id "
            "or the name under its icon (phone)."
        ),
    ),
]
ForceArg = Annotated[
    bool,
    Field(
        description=(
            "Desktop only: true kills the application, discarding unsaved work. "
            "Default false (a normal close)."
        )
    ),
]
UrlArg = Annotated[
    str,
    Field(min_length=1, max_length=_URL_MAX_CHARS, description="The URL."),
]
BrowserArg = Annotated[
    BrowserName,
    # Inline enum: the protocol enum's docstring would otherwise become the
    # schema's description (it names the runner).
    WithJsonSchema({"type": "string", "enum": [name.value for name in BrowserName]}),
    Field(
        description=(
            "Optional browser, default chromium. Changing it closes the other "
            "browser with its page state."
        )
    ),
]
SavePathArg = Annotated[
    str | None,
    Field(
        min_length=1,
        max_length=_PATH_MAX_CHARS,
        description=(
            "Optional absolute path to save the file; default the Downloads "
            "folder with the suggested file name."
        ),
    ),
]
LongPressDurationArg = Annotated[
    float | None,
    Field(
        ge=LONG_PRESS_MIN_SECONDS,
        le=LONG_PRESS_MAX_SECONDS,
        description=(
            f"Optional seconds to hold, {LONG_PRESS_MIN_SECONDS:g} to "
            f"{LONG_PRESS_MAX_SECONDS:g}."
        ),
    ),
]
DragDurationArg = Annotated[
    float | None,
    Field(
        ge=DRAG_MIN_SECONDS,
        le=DRAG_MAX_SECONDS,
        description=(
            f"Optional seconds the drag takes, {DRAG_MIN_SECONDS:g} to "
            f"{DRAG_MAX_SECONDS:g}; default {DEFAULT_DRAG_SECONDS:g}."
        ),
    ),
]
# The canonical modifier names (agents.pointer_modifiers); the Test Runner
# resolves primary per platform.
PointerModifierName = Literal["primary", "ctrl", "shift", "alt", "meta"]
ModifiersArg = Annotated[
    list[PointerModifierName] | None,
    Field(
        max_length=5,
        description=(
            "Optional (desktop): keys held during the action and released "
            "afterwards. primary = Cmd on macOS, Ctrl elsewhere; alt = Option "
            "on macOS; meta = Win / Super / Cmd."
        ),
    ),
]
SwipeDistanceArg = Annotated[
    float | None,
    Field(
        ge=SWIPE_MIN_DISTANCE,
        le=SWIPE_MAX_DISTANCE,
        description=(
            "Optional swipe length as a share of the screen side, "
            f"{SWIPE_MIN_DISTANCE:g} to {SWIPE_MAX_DISTANCE:g}."
        ),
    ),
]
SwipeDurationArg = Annotated[
    float | None,
    Field(
        ge=SWIPE_MIN_DURATION,
        le=SWIPE_MAX_DURATION,
        description=(
            f"Optional seconds the swipe takes, {SWIPE_MIN_DURATION:g} to "
            f"{SWIPE_MAX_DURATION:g}; short is a fling."
        ),
    ),
]
ButtonArg = Annotated[
    MobileButton,
    Field(description="The device button (back is Android only)."),
]
OrientationArg = Annotated[
    DeviceOrientation, Field(description="The orientation to rotate to.")
]
InstallPathArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=_PATH_MAX_CHARS,
        description=(
            "The app build on the computer the phone is attached to: an .apk, a "
            "simulator .app or a device-signed .ipa."
        ),
    ),
]
SearchQueryArg = Annotated[
    str,
    Field(
        min_length=1, max_length=_SEARCH_QUERY_MAX_CHARS, description="The web search."
    ),
]
WaitArg = Annotated[
    float,
    Field(
        gt=0,
        le=MCP_WAIT_MAX_SECONDS,
        description=f"Seconds to wait, more than 0 and at most {MCP_WAIT_MAX_SECONDS}.",
    ),
]


# --- browse_* arguments (docs/plan/feat-mcp-browser-toolset.md) -------------
# Inline enums: the protocol enums' docstrings would otherwise become the
# schemas' descriptions.
_WEB_BROWSER_ENUM = {"type": "string", "enum": [name.value for name in WebBrowser]}
WebBrowserArg = Annotated[
    WebBrowser,
    WithJsonSchema(_WEB_BROWSER_ENUM),
    Field(
        description=(
            "Required: the browser to open (chrome, edge, firefox or safari; "
            "browse_list_browsers shows which this test machine has)."
        )
    ),
]
OptionalWebBrowserArg = Annotated[
    WebBrowser | None,
    WithJsonSchema({"anyOf": [_WEB_BROWSER_ENUM, {"type": "null"}]}),
    Field(
        description=(
            "Optional browser (chrome, edge, firefox or safari). There is no "
            "default: without it the tool acts on the one browser that is open "
            "on the test machine; name it when several are open."
        )
    ),
]
OptionalBrowseUrlArg = Annotated[
    str | None,
    Field(
        min_length=1,
        max_length=_URL_MAX_CHARS,
        description="Optional URL to open (http, https, about or file).",
    ),
]
BrowseUrlArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=_URL_MAX_CHARS,
        description="The URL (http, https, about or file).",
    ),
]
HistoryActionArg = Annotated[
    WebHistoryAction,
    WithJsonSchema({"type": "string", "enum": [a.value for a in WebHistoryAction]}),
    Field(description="back, forward or reload."),
]
TabsActionArg = Annotated[
    WebTabsAction,
    WithJsonSchema({"type": "string", "enum": [a.value for a in WebTabsAction]}),
    Field(
        description=(
            "list the tabs, switch to the tab at index, close the tab at index "
            "(default the current one), or open a new tab."
        )
    ),
]
TabIndexArg = Annotated[
    int | None,
    Field(
        ge=0,
        le=WEB_TAB_INDEX_MAX,
        description="The tab index from browse_tabs list (switch; close).",
    ),
]
ScopeCssArg = Annotated[
    str | None,
    Field(
        min_length=1,
        max_length=WEB_SELECTOR_MAX_CHARS,
        description=(
            "Optional CSS selector: list only the elements inside it (page mode)."
        ),
    ),
]
ElementIndexArg = Annotated[
    int | None,
    Field(
        ge=0,
        le=MCP_BROWSE_INDEX_MAX,
        description=(
            "The element's index from the last browse_list_elements (list again "
            "after the page changes)."
        ),
    ),
]
ElementDescriptionArg = Annotated[
    str | None,
    Field(
        min_length=1,
        max_length=_DESCRIPTION_MAX_CHARS,
        description=(
            "The element in plain words (e.g. 'the Save button'); it is matched "
            "against the listed elements."
        ),
    ),
]
CssArg = Annotated[
    str | None,
    Field(
        min_length=1,
        max_length=WEB_SELECTOR_MAX_CHARS,
        description="A CSS selector (page mode only).",
    ),
]
XpathArg = Annotated[
    str | None,
    Field(
        min_length=1,
        max_length=WEB_SELECTOR_MAX_CHARS,
        description="An XPath expression (page mode only).",
    ),
]
BrowseTextArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=WEB_TYPE_TEXT_MAX_CHARS,
        description=(
            "The text to type into the element (not empty; spaces alone are "
            "typed as they are)."
        ),
    ),
]
ClearArg = Annotated[
    bool, Field(description="True: clear the field first. Default false.")
]
SelectValueArg = Annotated[
    str | None,
    Field(
        max_length=WEB_SELECT_VALUE_MAX_CHARS,
        description="The option's value attribute (give value or label).",
    ),
]
SelectLabelArg = Annotated[
    str | None,
    Field(
        max_length=WEB_SELECT_VALUE_MAX_CHARS,
        description="The option's visible text (give value or label).",
    ),
]
BrowseKeyArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=WEB_KEY_MAX_CHARS,
        description=(
            "A key or combination for the focused element, e.g. 'Enter', "
            "'Tab', 'Escape', 'ctrl+a'."
        ),
    ),
]
ReadWhatArg = Annotated[
    WebReadWhat,
    WithJsonSchema({"type": "string", "enum": [w.value for w in WebReadWhat]}),
    Field(
        description=(
            "text (the page's text), url, title, element_text (needs an "
            "element) or attribute (needs an element and name)."
        )
    ),
]
AttributeNameArg = Annotated[
    str | None,
    Field(
        min_length=1,
        max_length=WEB_ATTRIBUTE_NAME_MAX_CHARS,
        description="The attribute to read (what=attribute).",
    ),
]
WaitStateArg = Annotated[
    WebWaitState,
    WithJsonSchema({"type": "string", "enum": [w.value for w in WebWaitState]}),
    Field(description="present, visible or gone."),
]
BrowseTimeoutMsArg = Annotated[
    int,
    Field(
        ge=1,
        le=MCP_BROWSE_EVAL_TIMEOUT_MS_MAX,
        description=(
            f"Optional milliseconds, at most {MCP_BROWSE_EVAL_TIMEOUT_MS_MAX} "
            f"(default {WEB_TIMEOUT_MS_DEFAULT})."
        ),
    ),
]
ScriptArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MCP_BROWSE_SCRIPT_MAX_CHARS,
        description=(
            "JavaScript run in the page as a function body: use return for the "
            "result and arguments[0], arguments[1], ... for args."
        ),
    ),
]
ScriptArgsArg = Annotated[
    str | None,
    Field(
        min_length=1,
        max_length=MCP_BROWSE_ARGS_MAX_CHARS,
        description="Optional arguments as a JSON array, e.g. '[\"#id\", 3]'.",
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


# --- Machine tool requests -------------------------------------------------


def _both_or_neither(x: int | None, y: int | None) -> None:
    if (x is None) != (y is None):
        raise ValueError("[invalid] Give x and y together, or neither.")


class MachineRequest(_Dto):
    """The claim every machine tool runs inside. As a request of its own:
    list_commands, take_screenshot, browser_list_downloads, hide_keyboard."""

    pivot_id: int
    claim_token: str

    def agent_arguments(self) -> dict[str, object]:
        """The agent tool's arguments: the request's own fields, unset
        optional ones left out (the claim and ``browser`` are not
        arguments)."""
        return self.model_dump(
            mode="json",
            exclude={"pivot_id", "claim_token", "browser"},
            exclude_none=True,
        )


class ExecuteCommandRequest(MachineRequest):
    command: str
    cwd: str | None = None
    detach: bool = False


class ReadCommandOutputRequest(MachineRequest):
    session_id: str
    wait_seconds: int = 0


class SendCommandInputRequest(MachineRequest):
    session_id: str
    input_text: str


class StopCommandRequest(MachineRequest):
    session_id: str


class DescribedActionRequest(MachineRequest):
    """click, double_click, right_click, middle_click, hover."""

    description: str
    expected_result: str | None = None
    modifiers: list[PointerModifierName] | None = None


class ClickAtRequest(MachineRequest):
    """click_at, double_click_at, right_click_at, middle_click_at, hover_at."""

    x: int
    y: int
    expected_result: str | None = None
    modifiers: list[PointerModifierName] | None = None


class TypeTextRequest(MachineRequest):
    text: str


class KeyPressRequest(MachineRequest):
    keys: str


class DragRequest(MachineRequest):
    start_description: str
    end_description: str
    expected_result: str | None = None
    modifiers: list[PointerModifierName] | None = None


class DragAtRequest(MachineRequest):
    start_x: int
    start_y: int
    end_x: int
    end_y: int
    duration: float | None = None
    expected_result: str | None = None
    modifiers: list[PointerModifierName] | None = None


class ScrollRequest(MachineRequest):
    direction: ScrollDirection
    amount: int | None = None
    x: int | None = None
    y: int | None = None
    modifiers: list[PointerModifierName] | None = None

    @model_validator(mode="after")
    def _point(self) -> ScrollRequest:
        _both_or_neither(self.x, self.y)
        return self


class AppRequest(MachineRequest):
    """launch_app, check_app_installed, close_tab."""

    app_name: str


class CloseAppRequest(MachineRequest):
    app_name: str
    force: bool = False


class BrowserRequest(MachineRequest):
    """browser_get_page_content, browser_get_url."""

    browser: BrowserName = BrowserName.CHROMIUM


class BrowserNavigateRequest(BrowserRequest):
    url: str


class BrowserElementRequest(BrowserRequest):
    """browser_click, browser_double_click, browser_hover."""

    description: str


class BrowserTypeRequest(BrowserRequest):
    description: str
    text: str


class BrowserDownloadRequest(BrowserRequest):
    url: str
    save_path: str | None = None


class LongPressRequest(MachineRequest):
    description: str
    duration: float | None = None
    expected_result: str | None = None


class LongPressAtRequest(MachineRequest):
    x: int
    y: int
    duration: float | None = None
    expected_result: str | None = None


class SwipeRequest(MachineRequest):
    direction: ScrollDirection
    x: int | None = None
    y: int | None = None
    distance: float | None = None
    duration: float | None = None
    expected_result: str | None = None

    @model_validator(mode="after")
    def _point(self) -> SwipeRequest:
        _both_or_neither(self.x, self.y)
        return self


class PressButtonRequest(MachineRequest):
    button: MobileButton


class RotateDeviceRequest(MachineRequest):
    orientation: DeviceOrientation
    expected_result: str | None = None


class OpenUrlRequest(MachineRequest):
    url: str


class InstallAppRequest(MachineRequest):
    path: str


class WebSearchRequest(MachineRequest):
    query: str


class WaitRequest(MachineRequest):
    seconds: WaitArg


# --- browse_* requests ------------------------------------------------------


def _count_given(*values: object) -> int:
    return sum(value is not None for value in values)


class BrowseRequest(MachineRequest):
    """browse_list_browsers, browse_close; the base of the others: the
    optional browser (None: the one open browser)."""

    browser: WebBrowser | None = None


class BrowseOpenRequest(MachineRequest):
    browser: WebBrowser
    url: str | None = None


class BrowseNavigateRequest(BrowseRequest):
    url: str


class BrowseHistoryRequest(BrowseRequest):
    action: WebHistoryAction


class BrowseTabsRequest(BrowseRequest):
    action: WebTabsAction
    index: int | None = None

    @model_validator(mode="after")
    def _index(self) -> BrowseTabsRequest:
        if self.action == WebTabsAction.SWITCH and self.index is None:
            raise ValueError("[invalid] index is required to switch tabs.")
        if self.action in (WebTabsAction.LIST, WebTabsAction.NEW) and (
            self.index is not None
        ):
            raise ValueError("[invalid] index is only for switch and close.")
        return self


class BrowseListElementsRequest(BrowseRequest):
    scope_css: str | None = None


class BrowseTargetRequest(BrowseRequest):
    """An element named by exactly one of index, description, css, xpath
    (browse_click, browse_double_click, browse_hover)."""

    index: int | None = None
    description: str | None = None
    css: str | None = None
    xpath: str | None = None

    @model_validator(mode="after")
    def _one_target(self) -> BrowseTargetRequest:
        if _count_given(self.index, self.description, self.css, self.xpath) != 1:
            raise ValueError(
                "[invalid] Give exactly one of index, description, css or xpath."
            )
        return self


class BrowseTypeRequest(BrowseTargetRequest):
    # Not stripped: whitespace-only text is typed as it is.
    text: str = Field(max_length=WEB_TYPE_TEXT_MAX_CHARS)
    clear: bool = False

    @model_validator(mode="after")
    def _text_not_empty(self) -> BrowseTypeRequest:
        if not self.text:
            raise ValueError("[invalid] Give the text to type (it cannot be empty).")
        return self


class BrowseSelectRequest(BrowseTargetRequest):
    value: str | None = None
    label: str | None = None

    @model_validator(mode="after")
    def _one_option(self) -> BrowseSelectRequest:
        if _count_given(self.value, self.label) != 1:
            raise ValueError("[invalid] Give exactly one of value or label.")
        return self


class BrowsePressKeyRequest(BrowseRequest):
    key: str


class BrowseReadRequest(BrowseRequest):
    what: WebReadWhat
    index: int | None = None
    description: str | None = None
    css: str | None = None
    xpath: str | None = None
    name: str | None = None

    @model_validator(mode="after")
    def _target(self) -> BrowseReadRequest:
        given = _count_given(self.index, self.description, self.css, self.xpath)
        needs_element = self.what in (WebReadWhat.ELEMENT_TEXT, WebReadWhat.ATTRIBUTE)
        if needs_element and given != 1:
            raise ValueError(
                "[invalid] element_text and attribute need exactly one of index, "
                "description, css or xpath."
            )
        if not needs_element and given:
            raise ValueError(
                "[invalid] text, url and title read the whole page: give no element."
            )
        if (self.name is not None) != (self.what == WebReadWhat.ATTRIBUTE):
            raise ValueError("[invalid] name is required for, and only for, attribute.")
        return self


class BrowseWaitRequest(BrowseRequest):
    """An element named by exactly one of index, css, xpath (no
    description: a wait cannot match words to elements that do not exist yet)."""

    index: int | None = None
    css: str | None = None
    xpath: str | None = None
    state: WebWaitState
    timeout_ms: int = WEB_TIMEOUT_MS_DEFAULT

    @model_validator(mode="after")
    def _one_target(self) -> BrowseWaitRequest:
        if _count_given(self.index, self.css, self.xpath) != 1:
            raise ValueError("[invalid] Give exactly one of index, css or xpath.")
        return self


class BrowseEvaluateRequest(BrowseRequest):
    script: str
    args: str | None = None
    timeout_ms: int = WEB_TIMEOUT_MS_DEFAULT


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


class MachineInfo(_Dto):
    """get_project's ``machine`` block."""

    # desktop, android or ios.
    target: str
    connected: bool
    # The browse_* browsers of this test machine's target and OS (whether
    # one is open is live: browse_list_browsers); empty when unknown.
    web_browsers: list[str]


class ProjectResponse(_Dto):
    name: str
    manual_run: RunRef | None
    step_max_shots: int
    shot_max_bytes: int
    shot_max_pixels: int
    staged_shots_max: int
    capture: CaptureInfo
    # The test machine the machine tools drive (additive).
    machine: MachineInfo
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


# --- Machine tool responses -------------------------------------------------


class MachineActionResponse(_Dto):
    # The action's result in the test machine's words, neutralised; page,
    # command and search text fenced as data.
    text: str


class BrowseBrowserItem(_Dto):
    """One browser of the test machine's table (browse_list_browsers)."""

    # chrome, edge, firefox or safari.
    browser: str
    installed: bool
    # Open and controllable now.
    open: bool
    # page or accessibility while open; null otherwise.
    mode: str | None
    # shortcut (browse_open, or the person's test browser shortcut), app
    # (browse_open, or the person opens the app on the phone) or
    # open_tool_only (only browse_open).
    how_to_open: str
    version: str


class BrowseBrowsersResponse(_Dto):
    browsers: list[BrowseBrowserItem]
    note: str


class TakeScreenshotResponse(_Dto):
    # The real screen size the coordinates of click_at and the other *_at
    # tools, scroll and swipe refer to;
    # null when the test machine did not report it.
    width: int | None
    height: int | None
    # The size of the preview image returned with this result.
    preview_width: int
    preview_height: int
    note: str


class CommandResponse(_Dto):
    # None when the command ended and is no longer kept.
    session_id: str | None
    # Fenced as data; head and tail kept when long.
    output: str
    alive: bool
    exit_code: int | None
    # The test machine discarded output since the last read.
    output_dropped: bool
    # Kept for this claim (survives later commands); false on a test machine
    # whose software keeps only one command.
    kept: bool
    note: str


class CommandItem(_Dto):
    session_id: str
    started_at: datetime
    detached: bool
    # The command's start, data only.
    command_head: str
    # None when the test machine cannot say (it may have ended).
    alive: bool | None
    exit_code: int | None


class ListCommandsResponse(_Dto):
    commands: list[CommandItem]
    note: str
