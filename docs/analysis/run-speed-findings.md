# Run speed findings

Status: Collecting — add one section per analysed test run, then turn the
consolidated list into plans in `docs/plan/`.

Round 1 (`docs/plan/feat-run-speed-round-1.md`, `docs/changed/2026-10-28-run-speed-round-1.md`)
addressed F1, F2, F3, F4, F8, F9, F10 (modifier release; root cause still to
confirm, Q4), F11 in part (final turn on timeout) and Q3. Open: F5 (out of
scope: one model), F6, F7, F12, F13 (tracking).

Goal: find where case runs spend wall-clock time and what we can change to make
them faster, across several real test runs before deciding what to build.

## Consolidated findings

Ranked by payoff ÷ effort. "Seen in" lists the runs below that show it; add to it
as more runs are analysed.

| # | Finding | Fix idea | Effort | Seen in |
|---|---------|----------|--------|---------|
| F1 | **Bug:** `cwd` is not quoted. `agents/services/tools_controller.py::execute_command` builds `f"cd {cwd} && {command}"`, so a path with spaces is cut at the first space (`cd: /Users/.../Documents/Sencha: No such file or directory`). | `shlex.quote(cwd)` for POSIX runners; check what Windows runners need. | Tiny | C1514863 |
| F2 | Wording like "In macOS Terminal" / "fresh Terminal login shell" sends the sub-agent to the Terminal GUI (launch_app, cmd+t) instead of `execute_command`. That triggered a macOS "Accessibility Access (Events)" dialog, 14 wasted iterations and a Need help pause. | Planner: describe shell work as a command, never "in Terminal". Executor rules: a shell command always goes through `execute_command`; open a terminal window only when the step tests the terminal itself. | Small | C1514863 |
| F3 | Every sub-agent opens with a screenshot + vision call, also for shell-only steps (`sub_agent_prompts._build_state_section`: "Take a screenshot first"). This contradicts SHELL RULES, which say screenshots add nothing for shell commands. | Make the first screenshot conditional: skip it when the step is shell-only and the previous step's final observation is recent. | Small | C1514863 |
| F4 | Fixed cost per step (new sub-agent, 26 tool definitions, screenshot, 2–3 reasoning calls, RESULT turn) dominates short steps: about 50 s per one-line shell step. Look-only steps repeat checks an earlier step already made. | Let the planner merge consecutive read-only / state-switch steps (shell or look-only GUI) into one step (fits `PLAN_SCOPE_RULES` "combine repeat-safe actions"). | Small | C1514863, EXTJS-30117 |
| F5 | Reasoning effort is never sent, so every role (planner, sub-agent, vision Q&A, evaluate) runs at the model default. Reasoning summaries (`OPENAI_REASONING_SUMMARY=auto`) are on for every call. | Per-role reasoning effort (lower for routine sub-agent turns and vision Q&A). Measure before/after on the same case before changing defaults. | Medium | C1514863 |
| F6 | "Restoring state before step N" runs a full sub-agent (900 s budget, own screenshots and checks): about 2 min for the step 8 restore. | Deterministic state check first; fall back to an agent only when the check fails. Own plan. | Large | C1514863 |
| F7 | OmniParser could not find "Deny" on a macOS system (TCC) dialog; Escape did not dismiss it. | Executor rule: on system dialogs, try Tab/Space keyboard navigation before giving up; investigate whether system dialogs are parsed at all. | Small | C1514863 |
| F8 | **No scroll tool.** The agent tried PageDown, Cmd+Down, three Cmd+− zooms, a scrollbar drag, the green zoom button and Ctrl+Cmd+F before writing its own `ctypes` CGEvent scroll wheel. Step 4 timed out, the recovery failed and the case paused for help, only to bring the third editor into view. | Add a `scroll(direction, amount, at?)` desktop tool (pyautogui `scroll`/`hscroll`, GNOME backend equivalent) and mention it in the tool examples. | Small | EXTJS-30117 |
| F9 | **No coordinate click.** OmniParser has no element for "the end of this text line", window chrome (traffic lights, title bar, scrollbar) or blank page areas, so `click`/`drag`/`double_click` fail with "No OmniParser element matches". The vision model gave accurate screen coordinates every time, but the agent had to improvise: AppleScript `click at` (an AX press that does not move keyboard focus, so text went into the wrong editor), then Swift and clang (both broken by the machine's SDK mismatch), then a `ctypes` CGEvent click, which worked. | Add a `click_at(x, y)` tool (and maybe a text-offset target such as "end of element") that uses the runner's normal input path, so the click is a real mouse event that moves focus. | Medium | EXTJS-30117 |
| F10 | `type_text` on macOS opened the emoji picker and dropped or misplaced characters (" E1 typed" → "c 1ntent", " E2 typed" → only "pl" leaked) in three steps. Each case cost a failed attempt and a help pause. Not proven: possibly a modifier left down after `CTRL+CMD+F`, or typing right after an AX click that never focused the field. Typing after a real CGEvent click and AppleScript `keystroke` both worked. | Reproduce on the Mac runner: `key_press('ctrl+cmd+f')` followed by `type_text` with a leading space. If it is a stuck modifier, have the runner release all modifiers after every combo, and before `type_text`. | Medium | EXTJS-30117 |
| F11 | 150 s budgets are too tight for GUI steps on verified clicks. Four attempts timed out; two of them had already reached the expected state and were still verifying (step 5 attempt 3). | Budget GUI steps by their number of verified actions, or give "already-achieved" a cheaper exit (verify once, then answer). | Small | EXTJS-30117 |
| F12 | Vision answers sometimes contradict the screen ("Correction succeeded: Yes" while the text was unchanged; "caret is in Editor 2" vs "Editor 3 focused"). Each wrong answer costs one or more extra loops. | Ask narrower questions in the prompt rules (exact text of one field); consider a cheaper second check or a DOM / accessibility read where one exists. | Medium | EXTJS-30117 |
| F13 | The cost of a help pause is human waiting time, which dwarfs everything else. This run paused 6 times (5 need_help + 1 runner fault) for 8 steps, and most pauses came from F8–F10, not from the product. | Fixing F8–F10 removes most pauses. Track the pause count per run as a metric. | — | C1514863, EXTJS-30117 |

## Open questions

- Q1 (C1514863): step 3 was resumed twice. The first resume stopped mid-click on
  the macOS "Open" security dialog, and a second resume ran a full restore. Was
  that a person pressing Continue twice, or a worker fault? Check the run's help
  requests and worker logs.
- Q2: we have no per-step timestamps in the log. Add elapsed time per step and per
  LLM call to the orchestrator log so later analyses can measure, not estimate.
- Q3 (EXTJS-30117): the verdict was FAIL although every step finally reached its
  expected result. Probably step 5, whose last attempt timed out after it had
  succeeded (decision `continue`, cause agent). Should a `continue` with "the
  expected state was achieved" count as passed for the verdict? This is a
  correctness question, not a speed one, but it makes the run look worse than it was.
- Q4 (EXTJS-30117): root cause of the emoji picker / lost keystrokes (F10).

## Runs analysed

### C1514863 — Sencha Cmd 8.1.0.42 side-by-side install and switch (macOS)

Date analysed: 2026-10-28. Model: gpt-5.6-sol. 13 planned steps; 2 help pauses,
1 result-review redesign (SDK archive swapped to ext-8.0.0.43), stopped at
step 9 on a second help pause.

Timeline (from screenshot clocks; no step timestamps in the log):
- Steps 9–13 (first pass) took about 4 minutes, roughly 50 s per one-line shell step.
- Restore before step 8 took about 2 minutes; step 8 itself about 1 minute.

Per-step notes:
- Steps 1, 4–7, 11–12: single shell commands, each its own sub-agent with an
  opening screenshot (F3, F4). 5→6→7 and 11→12 could have been one step each.
- Step 2: agent went to the Terminal GUI because of the wording (F2), hit the
  Accessibility dialog, 16 tool calls for one `ditto` command.
- Step 3: blocked by the same dialog → Need help #1 (F2, F7). Resumed twice (Q1).
- Step 8: supplied SDK archive did not match the plan's expected layout →
  Need help #2 (test data, not speed).
- Steps 10, 13: `cwd` with a space broke (F1); the agent recovered with a
  quoted `cd`, but the app did not exist anyway.
- After the redesign, step 9 failed with "Failed creating background process"
  during Sass → Need help #3 (environment, not speed).

Not speed-related but noted: `sencha --version` prints "No such property :
version" and exits 65 on both Cmd versions; the agent correctly ignored it.

### EXTJS-30117 — three Froala editor fields in one form panel (real Safari, macOS)

Date analysed: 2026-10-28. Model: gpt-5.6-sol. 8 planned steps, 142 iterations,
6 pauses (5 need_help + 1 Test Runner stopped responding), verdict FAIL (Q3).
No product defect was found.

Per-step notes:
- Step 1: clean (7 tool calls). Its screenshot already read the title, the
  status panel text and the editors, which makes step 3 redundant (F4).
- Step 2: the Develop menu worked; "Close Web Inspector" needed a reworded
  description after the first OmniParser miss.
- Step 3: one screenshot, repeats step 1 (F4).
- Step 4: Editor 3 was below the fold and the agent had no way to scroll (F8):
  PageDown/Cmd+Down did nothing without page focus, there was no blank-area or
  scrollbar target (F9), zoom-out three times, fullscreen. Timeout → recovery
  timeout → help pause #1. After help: done in 7 calls.
- Step 5: three attempts. #1 type_text opened the emoji picker and corrupted
  Editor 1 (F10) → help #2. #2 OmniParser could not target "end of text" (F9),
  timeout → help #3. #3 improvised AppleScript clicks, Swift (SDK mismatch),
  clipboard paste and finally set the AX value directly; it succeeded but timed
  out still verifying (F11), decision continue.
- Step 6: #1 the AppleScript coordinate click did not move focus and type_text
  opened the emoji picker again (F9, F10) → help #4. #2 Test Runner stopped
  responding → runner fault pause #5 → restore sub-agent (cheap here: one
  screenshot). #3 typed into Editor 1 instead (AX click kept the old focus), undo,
  then AX focus + AppleScript `keystroke` worked (16 calls).
- Step 7: #1 the same misrouting and emoji picker, stray "pl" in Editor 2, timeout
  → help #6. #2 a `ctypes` CGEvent click + type_text worked first time; a
  `ctypes` scroll wheel event also worked (shows F8/F9 are cheap to provide).
- Step 8: the first click hit the floating status badge instead of "Re-check" (both
  green, both bottom-right); the second description worked. The planner's
  wording "bottom toolbar" did not match the page.

Fastest path in hindsight: steps 1–4 as one look-only step with a scroll, and
steps 5–7 each as a real click_at + type_text. That is about 8–10 tool calls
per step and no pauses.

### <next test> — <title>

Date analysed: YYYY-MM-DD. Model: …

Timeline:
- …

Per-step notes:
- … (reference F-numbers above; add new F rows for new findings)
