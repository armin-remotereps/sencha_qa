"""The person window's dark palette, font names and ttk theme.

One place for every colour the window, the mark sheet and the chat use (the
dark tokens of ``static/css/punk-hazard.css``; the web's translucent tints are
pre-blended here, Tk has no alpha). ``apply_dark_theme`` runs once, right
after the root is created and before any widget, so the option database and
the ttk styles are in place for every widget and the first map is already
dark. The theme is always on: no light theme, no OS appearance following.
"""

from typing import Final

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:  # the window process reports a missing tkinter itself
    pass

# --- palette ------------------------------------------------------------------

PAGE: Final[str] = "#0b1118"
SURFACE: Final[str] = "#131d28"
RAISED: Final[str] = "#1a2733"
HOVER: Final[str] = "#223240"
BORDER: Final[str] = "#2d3d4b"
BORDER_STRONG: Final[str] = "#3d5060"
TEXT: Final[str] = "#f5f8fa"
MUTED: Final[str] = "#9baab6"
PLACEHOLDER: Final[str] = "#6d7d8a"
OK: Final[str] = "#2fbf71"
WARN: Final[str] = "#f2b84b"
ERROR: Final[str] = "#ef5b62"
INFO: Final[str] = "#4da3ff"
CYAN: Final[str] = "#2ecae4"
CODE_BG: Final[str] = "#0a0f15"
CODE_TEXT: Final[str] = "#c8d6e0"
# Pre-blended tints. The covered / current step: warning at 30 % over the
# surface, with TEXT on top.
HIGHLIGHT_BG: Final[str] = "#564c33"
# Chat bubbles: people get cyan at 20 % over the text background (PAGE); the
# agent the raised surface.
USER_BUBBLE_BG: Final[str] = "#123641"
AGENT_BUBBLE_BG: Final[str] = RAISED
# Selected text and list rows: info at 35 % over PAGE.
SELECTION_BG: Final[str] = "#224469"

# --- named fonts (created by PersonWindowApp, scaled by A−/A+) -----------------

HEADING_FONT: Final[str] = "PunkHazardHeading"
SUBHEADING_FONT: Final[str] = "PunkHazardSubheading"
BOLD_FONT: Final[str] = "PunkHazardBold"
ITALIC_FONT: Final[str] = "PunkHazardItalic"
BOLD_ITALIC_FONT: Final[str] = "PunkHazardBoldItalic"

# The section rail's buttons (person_window_scroll).
RAIL_STYLE: Final[str] = "Rail.TButton"

# Classic Tk widgets (tk.Text, tk.Canvas, tk.Listbox and the combobox's list):
# option database entries, set before any widget exists.
_TEXT_OPTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("background", PAGE),
    ("foreground", TEXT),
    ("insertBackground", TEXT),
    ("selectBackground", SELECTION_BG),
    ("selectForeground", TEXT),
    ("inactiveSelectBackground", SELECTION_BG),
    ("highlightBackground", BORDER),
    ("highlightColor", CYAN),
)


def apply_dark_theme(root: "tk.Tk") -> None:
    """Dark ttk styles and Tk options for every widget created afterwards.

    ``clam`` on every platform: aqua (macOS) and vista (Windows) ignore most
    colours.
    """
    root.configure(background=SURFACE)
    for widget_class in ("Text", "Listbox", "TCombobox*Listbox"):
        for option, value in _TEXT_OPTIONS:
            root.option_add(f"*{widget_class}.{option}", value)
    root.option_add("*Canvas.background", SURFACE)
    root.option_add("*Canvas.highlightBackground", SURFACE)
    root.option_add("*Canvas.highlightColor", SURFACE)

    style = ttk.Style(root)
    style.theme_use("clam")
    _configure_base(style)
    _configure_buttons(style)
    _configure_notebook(style)
    _configure_inputs(style)
    _configure_scrollbar(style)
    _configure_choices(style)


def _configure_base(style: "ttk.Style") -> None:
    style.configure(
        ".",
        background=SURFACE,
        foreground=TEXT,
        fieldbackground=PAGE,
        bordercolor=BORDER,
        lightcolor=SURFACE,
        darkcolor=SURFACE,
        troughcolor=PAGE,
        focuscolor=CYAN,
        selectbackground=SELECTION_BG,
        selectforeground=TEXT,
        insertcolor=TEXT,
        arrowcolor=MUTED,
    )
    style.map(
        ".",
        foreground=[("disabled", PLACEHOLDER)],
        background=[("disabled", SURFACE)],
    )
    style.configure("TFrame", background=SURFACE)
    style.configure("TLabel", background=SURFACE, foreground=TEXT)


def _configure_buttons(style: "ttk.Style") -> None:
    style.configure(
        "TButton",
        background=RAISED,
        foreground=TEXT,
        bordercolor=BORDER_STRONG,
        lightcolor=RAISED,
        darkcolor=RAISED,
        focuscolor=CYAN,
        padding=(10, 4),
    )
    style.map(
        "TButton",
        background=[("disabled", SURFACE), ("pressed", BORDER), ("active", HOVER)],
        foreground=[("disabled", PLACEHOLDER)],
        bordercolor=[("disabled", BORDER), ("focus", CYAN)],
        lightcolor=[("disabled", SURFACE), ("pressed", BORDER), ("active", HOVER)],
        darkcolor=[("disabled", SURFACE), ("pressed", BORDER), ("active", HOVER)],
    )
    style.configure(
        RAIL_STYLE,
        background=SURFACE,
        foreground=MUTED,
        bordercolor=SURFACE,
        lightcolor=SURFACE,
        darkcolor=SURFACE,
        anchor="w",
        padding=(8, 2),
        # clam gives buttons a minimum width of 11 characters; the rail's
        # are as wide as their labels.
        width=0,
        relief="flat",
    )
    style.map(
        RAIL_STYLE,
        background=[("pressed", BORDER), ("active", HOVER)],
        foreground=[("active", TEXT)],
        bordercolor=[("active", HOVER)],
        lightcolor=[("pressed", BORDER), ("active", HOVER)],
        darkcolor=[("pressed", BORDER), ("active", HOVER)],
    )


def _configure_notebook(style: "ttk.Style") -> None:
    style.configure(
        "TNotebook",
        background=SURFACE,
        bordercolor=BORDER,
        lightcolor=SURFACE,
        darkcolor=SURFACE,
    )
    style.configure(
        "TNotebook.Tab",
        background=PAGE,
        foreground=MUTED,
        bordercolor=BORDER,
        lightcolor=PAGE,
        darkcolor=PAGE,
        padding=(10, 4),
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", SURFACE), ("active", HOVER)],
        foreground=[("selected", TEXT), ("active", TEXT)],
        lightcolor=[("selected", SURFACE), ("active", HOVER)],
        darkcolor=[("selected", SURFACE), ("active", HOVER)],
    )


def _configure_inputs(style: "ttk.Style") -> None:
    for name in ("TEntry", "TCombobox"):
        style.configure(
            name,
            fieldbackground=PAGE,
            background=RAISED,
            foreground=TEXT,
            bordercolor=BORDER,
            lightcolor=PAGE,
            darkcolor=PAGE,
            insertcolor=TEXT,
            selectbackground=SELECTION_BG,
            selectforeground=TEXT,
            arrowcolor=MUTED,
        )
        style.map(
            name,
            fieldbackground=[("readonly", PAGE), ("disabled", SURFACE)],
            foreground=[("disabled", PLACEHOLDER), ("readonly", TEXT)],
            bordercolor=[("focus", CYAN)],
            lightcolor=[("focus", PAGE)],
            background=[("active", HOVER), ("pressed", BORDER)],
            selectbackground=[("readonly", SELECTION_BG)],
            selectforeground=[("readonly", TEXT)],
            arrowcolor=[("disabled", PLACEHOLDER), ("active", TEXT)],
        )


def _configure_scrollbar(style: "ttk.Style") -> None:
    style.configure(
        "TScrollbar",
        background=RAISED,
        troughcolor=SURFACE,
        bordercolor=SURFACE,
        lightcolor=RAISED,
        darkcolor=RAISED,
        arrowcolor=MUTED,
        gripcount=0,
    )
    style.map(
        "TScrollbar",
        background=[("pressed", BORDER_STRONG), ("active", HOVER)],
        lightcolor=[("pressed", BORDER_STRONG), ("active", HOVER)],
        darkcolor=[("pressed", BORDER_STRONG), ("active", HOVER)],
        arrowcolor=[("disabled", PLACEHOLDER), ("active", TEXT)],
    )


def _configure_choices(style: "ttk.Style") -> None:
    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(
            name,
            background=SURFACE,
            foreground=TEXT,
            indicatorbackground=PAGE,
            indicatorforeground=CYAN,
            upperbordercolor=BORDER_STRONG,
            lowerbordercolor=BORDER_STRONG,
            focuscolor=CYAN,
        )
        style.map(
            name,
            background=[("active", SURFACE)],
            foreground=[("disabled", PLACEHOLDER)],
            indicatorbackground=[
                ("disabled", SURFACE),
                ("pressed", HOVER),
                ("selected", PAGE),
            ],
            indicatorforeground=[("disabled", PLACEHOLDER)],
        )
