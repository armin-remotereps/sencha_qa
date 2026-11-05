"""A scrollable page of the person window with an optional section rail.

``ScrollPage`` is a canvas with an inner frame: the page asks for its
content's height up to what the screen leaves (minus ``allowance`` for what
sits above it), so a long page scrolls instead of growing off the screen, and
its scrollbar shows only when the content does not fit.

A page that registers sections (``add_section``) gets a column of buttons on
its right, one per section that is shown, that jumps the page to that
section. The buttons hide while the page fits or fewer than two sections are
shown, but the column keeps its width (``minsize``, never shrinking for one
text size): collapsing it would change the requested width, every wrap
length and the content height, which could flip the rail on and off in a
loop. "Shown" never relies on the window being mapped (it is withdrawn most
of the time): a widget counts when it and every ancestor up to the page frame
has a geometry manager and it asks for some height.

The rails are refreshed by the window, not by each page: ``schedule_refresh``
asks the window (``request_refresh``) for its one debounced refresh, which
settles the layout once and calls ``refresh_sections(settle=False)`` on every
page.
"""

from collections.abc import Callable
from typing import Final, Protocol

from controller_client.person_window_theme import RAIL_STYLE

try:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import ttk
except ImportError:  # the window process reports a missing tkinter itself
    pass

# The pages' default width (the window is about 80 px wider, plus the rail).
PAGE_WIDTH: Final[int] = 560
# Room left for the title bar, menu bar and taskbar when the content is tall,
# plus the header and the notebook's tabs above a page.
SCREEN_MARGIN: Final[int] = 160
HEADER_ALLOWANCE: Final[int] = 150
# One wheel step of a page.
WHEEL_STEP_PX: Final[int] = 20
# Room kept above a section the rail jumps to.
_JUMP_MARGIN_PX: Final[int] = 6
# Rail button: style padding (2 × 8) plus ttk's border and focus ring, and
# the rail frame's own padding.
_RAIL_BUTTON_EXTRA_PX: Final[int] = 24
_RAIL_PADDING: Final[tuple[int, int, int, int]] = (4, 12, 4, 12)
_RAIL_FONT: Final[str] = "TkDefaultFont"


class SectionPage(Protocol):
    """What a module that builds into a page needs (``ScrollPage``)."""

    outer: "ttk.Frame"
    frame: "ttk.Frame"

    def add_section(self, label: str, *widgets: "tk.Misc") -> None: ...

    def refit_height(self) -> None: ...


class ScrollPage:
    """A vertically scrollable page with an optional section rail."""

    def __init__(
        self,
        parent: "tk.Misc",
        *,
        request_refresh: Callable[[], None],
        allowance: int | Callable[[], int] = HEADER_ALLOWANCE,
        on_rail_resize: Callable[[], None] | None = None,
    ) -> None:
        self._request_refresh = request_refresh
        self._allowance = allowance
        self._on_rail_resize = on_rail_resize
        outer = ttk.Frame(parent)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)
        background = ttk.Style(outer).lookup("TFrame", "background")
        canvas = tk.Canvas(
            outer,
            borderwidth=0,
            highlightthickness=0,
            yscrollincrement=WHEEL_STEP_PX,
            width=PAGE_WIDTH,
        )
        if background:
            canvas.configure(background=background)
        scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=self._on_canvas_scroll)
        canvas.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        frame = ttk.Frame(canvas, padding=12)
        frame.columnconfigure(0, weight=1)
        item = canvas.create_window(0, 0, window=frame, anchor="nw")
        frame.bind("<Configure>", self._on_content_configure)
        canvas.bind(
            "<Configure>", lambda event: canvas.itemconfigure(item, width=event.width)
        )
        self.outer = outer
        self.canvas = canvas
        self.scrollbar = scroll
        self.frame = frame
        # The rail: created with the first section.
        self.rail: ttk.Frame | None = None
        self._sections: list[tuple[str, tuple[tk.Misc, ...]]] = []
        self._fits = True
        self._shown: tuple[str, ...] | None = None
        self._buttons_visible = False
        self._rail_width = 0
        # The rail font's "0" width the rail width was measured with.
        self._measured_with = 0

    # --- sections -------------------------------------------------------------

    def add_section(self, label: str, *widgets: "tk.Misc") -> None:
        """Register a section in page order; it is shown when any of
        ``widgets`` is shown (several share one row, e.g. swapped frames)."""
        if not widgets:
            raise ValueError("a section needs at least one widget")
        if self.rail is None:
            rail = ttk.Frame(
                self.outer, padding=_RAIL_PADDING, width=self._rail_width, height=1
            )
            # The rail never sizes the page: its buttons showing (only when
            # the page does not fit) must not make the page taller or wider,
            # or the window could grow until the page fits, hide the buttons,
            # shrink and show them again. The column's minsize reserves the
            # width and sticky="ns" stretches the rail to the page's height.
            rail.pack_propagate(False)
            rail.grid(row=0, column=2, sticky="ns")
            self.rail = rail
        self._sections.append((label, widgets))
        self.schedule_refresh()

    def sections(self) -> tuple[str, ...]:
        """Every registered section's label, in page order."""
        return tuple(label for label, _widgets in self._sections)

    def shown_sections(self) -> tuple[str, ...]:
        """The sections shown now, in page order."""
        return tuple(
            label
            for label, widgets in self._sections
            if any(self._is_shown(widget) for widget in widgets)
        )

    def rail_width(self) -> int:
        """Width reserved for the rail (0 without sections)."""
        return self._rail_width

    def schedule_refresh(self) -> None:
        """Ask the window for its debounced rail refresh; a no-op without
        sections."""
        if self._sections:
            self._request_refresh()

    def refresh_sections(self, *, settle: bool = True) -> None:
        """Show a button per shown section; hide them while the page fits.
        ``settle=False``: the caller ran ``update_idletasks`` already."""
        rail = self.rail
        if rail is None:
            return
        if settle:
            self.outer.update_idletasks()
        self._update_rail_width()
        shown = self.shown_sections()
        if shown != self._shown:
            # Rebuilt only when the shown labels change; created unpacked.
            self._shown = shown
            for child in rail.winfo_children():
                child.destroy()
            for label in shown:
                ttk.Button(
                    rail,
                    text=label,
                    style=RAIL_STYLE,
                    takefocus=False,
                    command=self._jump_command(label),
                )
            self._buttons_visible = False
        visible = not self._fits and len(shown) >= 2
        if visible != self._buttons_visible:
            self._buttons_visible = visible
            for child in rail.winfo_children():
                if visible:
                    child.pack(side="top", fill="x")
                else:
                    child.pack_forget()
        if visible:
            # The column never shrinks for one text size: grow it to the
            # buttons if the estimate was short. The rail's own reqwidth is
            # fixed (no propagation), so measure its buttons.
            rail.update_idletasks()
            widest = max(
                (child.winfo_reqwidth() for child in rail.winfo_children()), default=0
            )
            self._set_rail_width(widest + _RAIL_PADDING[0] + _RAIL_PADDING[2])

    def scroll_to(self, label: str) -> bool:
        """Scroll the page so the section's first shown widget is at the top;
        False when the section is unknown or not shown."""
        for name, widgets in self._sections:
            if name != label:
                continue
            for widget in widgets:
                if self._is_shown(widget):
                    self.outer.update_idletasks()
                    height = self.frame.winfo_height()
                    if height <= 1:
                        height = self.frame.winfo_reqheight()
                    if height <= 1:
                        return False
                    top = max(0, self._offset(widget) - _JUMP_MARGIN_PX)
                    self.canvas.yview_moveto(top / height)
                    return True
            return False
        return False

    def _jump_command(self, label: str) -> Callable[[], None]:
        def jump() -> None:
            self.scroll_to(label)

        return jump

    def _is_shown(self, widget: "tk.Misc") -> bool:
        if widget.winfo_reqheight() <= 1:
            return False
        page = str(self.frame)
        current: tk.Misc | None = widget
        while current is not None and str(current) != page:
            if not current.winfo_manager():
                return False
            current = current.master
        return current is not None

    def _offset(self, widget: "tk.Misc") -> int:
        """The widget's y inside the page frame."""
        page = str(self.frame)
        y = 0
        current: tk.Misc | None = widget
        while current is not None and str(current) != page:
            y += current.winfo_y()
            current = current.master
        return y

    def _update_rail_width(self) -> None:
        font = tkfont.nametofont(_RAIL_FONT, root=self.outer)
        unit = font.measure("0")
        if unit == self._measured_with:
            return
        # A new text size: measure again from the labels (may shrink).
        self._measured_with = unit
        widest = max(
            (font.measure(label) for label, _widgets in self._sections), default=0
        )
        padding = _RAIL_PADDING[0] + _RAIL_PADDING[2]
        self._set_rail_width(widest + _RAIL_BUTTON_EXTRA_PX + padding, force=True)

    def _set_rail_width(self, width: int, *, force: bool = False) -> None:
        if width == self._rail_width or (not force and width < self._rail_width):
            return
        self._rail_width = width
        self.outer.columnconfigure(2, minsize=width)
        if self.rail is not None:
            self.rail.configure(width=width)
        if self._on_rail_resize is not None:
            self._on_rail_resize()

    # --- scrolling --------------------------------------------------------------

    def _allowance_px(self) -> int:
        allowance = self._allowance
        return allowance() if callable(allowance) else allowance

    def _on_content_configure(self, _event: "tk.Event[ttk.Frame]") -> None:
        self.refit_height()

    def refit_height(self) -> None:
        """Ask for the content's height up to what the screen leaves (run
        again when the allowance changed)."""
        canvas = self.canvas
        frame = self.frame
        canvas.configure(scrollregion=canvas.bbox("all"))
        max_height = canvas.winfo_screenheight() - SCREEN_MARGIN - self._allowance_px()
        canvas.configure(
            width=max(PAGE_WIDTH, frame.winfo_reqwidth()),
            height=min(frame.winfo_reqheight(), max_height),
        )
        self.schedule_refresh()

    def _on_canvas_scroll(self, first: float | str, last: float | str) -> None:
        # Tk passes the fractions as strings.
        self.scrollbar.set(first, last)
        fits = float(first) <= 0.0 and float(last) >= 1.0
        if fits:
            self.scrollbar.grid_remove()
        else:
            self.scrollbar.grid()
        if fits != self._fits:
            self._fits = fits
            self.schedule_refresh()
