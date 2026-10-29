# Test Runner person window scrolls

Date: 2026-10-29
Plan: inline (small change)

## What changed
- `controller_client/person_window_app.py`: the window's content sits in a
  vertically scrollable Canvas (`_build_scroll_area`). The window asks for its
  content's height up to the screen height minus `_SCREEN_MARGIN`; the scrollbar
  shows only when the content does not fit; a new step or round scrolls to the top.
- Mouse wheel scrolls the page (`_on_wheel`, `_scroll_page`; `<MouseWheel>` on
  Windows/macOS, `Button-4/5` on X11) in `_WHEEL_STEP_PX` steps.

## Why
A step with a long how-to list or an open Ask the AI section made the window
taller than the screen, so its buttons were out of reach.

## Behaviour and contract changes
- `CLIENT_VERSION` 0.10.0 → 0.10.1 (no protocol change): the bump is what makes
  the server offer Update Test Runner to connected 0.10.0 runners.

## Notes for future changes
- Any runner-side change needs a `CLIENT_VERSION` bump, or no runner is offered
  the update (`projects/runner_update_offer.runner_is_older`).
- The wheel is left to widgets that scroll themselves: a Text box with more
  content than fits, a Text box's own scrollbar, and the picker's open list
  (a Tk-created `.popdown` widget whose `event.widget` is a plain path string).
- The picker has its own wheel binding that scrolls the page and returns
  "break": the ttk Combobox class binding would otherwise switch the selected step.
- Windows deltas are summed until a full notch (120), so precision touchpads
  do not jump; Shift+wheel (macOS horizontal swipe) is ignored.
