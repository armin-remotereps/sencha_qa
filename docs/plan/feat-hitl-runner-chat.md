# feat: Answer Human in the loop steps from the Test Runner

Status: In progress
Created: 2026-10-28
Request: In a Human in the loop (HITL) run a person performs each step on the
test machine, but today the instructions and the report form ("Your turn" card)
are only in the Punk Hazard web UI, so the person juggles two devices. The user
wants the Test Runner (`controller_client/`, already running on that test
machine) to show these messages and take the person's answers, so the whole
loop can be driven from the test machine. "And so..." leaves open how far this
goes (Need help requests, help chat and result chat); this plan recommends a v1
of person steps only and designs the protocol so the rest can follow.

## Context

- **HITL run mode** (`docs/changed/2026-10-28-hitl-run-mode.md`,
  `docs/plan/feat-hitl-run-mode.md`). The worker pauses the run in
  `projects/services.py::_pause_run_for_person`, opening or reopening a
  `CasePersonStep` row (`projects/person_step_rows.py::open_person_step_row`;
  a "not confirmed" redo reuses the row and bumps `round`). The person acts
  through `projects/person_step_services.py`: `report_person_step`,
  `overrule_person_step`, `mark_failed_person_step` and
  `mark_blocked_person_step`. Each takes `user: CustomUser` and locks the
  project, the run, the step and then the case. Each refuses unless the run is
  PAUSED and the step id is open (`waiting`/`not_confirmed`), then queues the
  pending cases, broadcasts after commit with `broadcast_run_and_pivots` and
  `broadcast_project_attention_changed`, and returns `PersonStepActionResult`.
  Views (`projects/person_step_views.py`) validate `PersonStepReportForm` or
  `PersonStepDecisionForm` (`projects/forms.py`).
- **Identity in person steps.** `reported_by`/`closed_by` are nullable user FKs.
  `person_label(user)` (`projects/case_result_text.py`) fills the checkpoint
  `reporter` string. The orchestrator uses that string in summaries
  (`agents/services/orchestrator.py` ~910/943/1222), and the verdict text says
  "Marked failed by <name>". `_settle_row` (`person_step_rows.py`) infers the
  close reason OVERRULED from `closed_by__isnull=False`. **An overrule with no
  user would be recorded as CONFIRMED**, so this must change.
- **Several paused runs.** `list_reopen_blockers` has `_has_other_started_run`:
  a project can have one STARTED run and other PAUSED ones. Several HITL runs
  can each hold an open step at the same time.
- **Web sync.** `templates/projects/test_run_case_detail.html` (~878) reloads
  the case page whenever the case moves into or out of `awaiting_person`. A
  report made anywhere therefore updates the web card through the existing
  `broadcast_run_and_pivots`.
- **Test Runner today.** It is a terminal-only asyncio program
  (`controller_client/main.py`, `client.py`) with no GUI. It connects to
  `ws/controller/` (`projects/controller_consumer.py::ControllerConsumer`) and
  authenticates with the project's Connection key (`handshake`, then
  `claim_agent_connection`; newest connection wins and the old one is closed
  with `SUPERSEDED`). Server→runner messages are actions that the consumer
  forwards from the channel group `controller_<project_id>`
  (`controller_<action>` handlers). Runner→server messages are replies routed
  through `ReplyTracker`, and unknown types are silently dropped
  (`_route_authenticated_message`). The runner handles one message at a time:
  `_receive_messages` awaits each handler. Its protocol is
  `controller_client/protocol.py`, which is stdlib-only and imported by the
  server: `CLIENT_VERSION = "0.7.0"`, `ClientCapability`,
  `REQUIRED_CLIENT_CAPABILITIES`, and
  `CLIENT_CAPABILITIES = tuple(ClientCapability)` (everything is always
  advertised). Optional capabilities are checked where they are used
  (`projects/runner_capability_services.py::runner_supports`).
- **Runner platforms.** Windows and macOS use pyautogui. Linux supports only
  GNOME on Wayland (Mutter RemoteDesktop/ScreenCast,
  `docs/changed/2026-10-28-wayland-controller.md`). Python 3.13 runs in its own
  venv created from the system `python3`/`python`
  (`controller_client/scripts/setup.sh`, `setup.ps1`, `setup.bat`). It has its
  own strict mypy config (`controller_client/setup.cfg`) and local stubs.
  `close_app`/`close_tab` never close the runner's own process or its hosts
  (`app_close_policy.py::RunnerProcesses`: own pid plus ancestors).
- **tkinter availability** (checked): the python.org macOS framework build
  here has Tk 8.6 (`controller_client/.venv/bin/python -c "import tkinter"`
  gives 8.6). python.org Windows installers ship it by default (it is an
  optional installer component). Homebrew Python needs `python-tk@3.13`.
  Ubuntu needs `apt install python3-tk`. Tk has no native Wayland backend and
  runs through XWayland on GNOME. On macOS, Tk/AppKit must own the process's
  main thread, so it cannot share the runner's asyncio main thread or run in a
  worker thread. A separate process is the documented workaround.
- **Controller ZIP** (`projects/services.py::generate_controller_client_zip`)
  ships every tracked file under `controller_client/`, so new runner modules
  are included automatically.

## Goals
- A person at the test machine sees the open person step of their project's
  HITL run in a small Test Runner window. The window shows the instruction,
  the expected result, the command (with Copy) and its shell, the round and
  the agent's objection. From there the person can Report (as expected or
  different, with a note and the command output), Overrule, Mark failed and
  Mark blocked, with the same rules and effects as the web card.
- The web card and the runner window stay in sync both ways. The first answer
  wins and a stale answer is refused with a clear message.
- The window never shows up in the agent's screenshots, the vision parse or
  the confirm agent's checks. It never takes part in agent actions.
- Old runners keep working unchanged (web only). A new runner on an old
  server never shows the window.

## Non-goals
- Need help requests (Continue / Mark failed / Mark blocked), help chat and
  result chat in the runner (Q1; follow-ups designed for, not built).
- "Continue run" and "Abort run" from the runner. When the queue is refused
  (gate), the window shows the reason and points to the run page.
- Linking a runner to a user account (Q3).
- A terminal-prompt or localhost web UI fallback (Q2).
- Any change to the agent, the confirm sub-agent or the checkpoint format.

## Design

### Data model
`projects/models.py`, migration `0039_person_step_channel` (additive):

- New `PersonStepChannel(TextChoices)`: `web` ("Punk Hazard"), `runner`
  ("Test Runner").
- `CasePersonStep.reported_via = CharField(max_length=8, choices=PersonStepChannel.choices, blank=True, default="")`:
  set with every report.
- `CasePersonStep.closed_via` is the same kind of field. It is set by an
  overrule, Mark failed or Mark blocked.
- `""` means "not by a person yet". Existing rows keep `""`, and their
  `reported_by`/`closed_by` still identify the web user. Adding nullable or
  defaulted columns to the populated table is a metadata-only change on
  Postgres 11+.

No new user, no "runner identity" row: the runner channel is anonymous (Q3).

### Shared code (written first, before the parallel parts)
1. **`controller_client/protocol.py`** (stdlib only; the server imports it).
   This is a shared touch point with `docs/plan/feat-runner-self-update.md`
   (see Risks).
   - `CLIENT_VERSION` "0.7.0" becomes "0.8.0" (coordinate with self-update:
     whichever lands second takes the next minor).
   - `ClientCapability.PERSON_STEPS_V1 = "person_steps_v1"`: optional, not in
     `REQUIRED_CLIENT_CAPABILITIES`.
   - `CLIENT_CAPABILITIES` stays "everything this build knows". New
     `advertised_capabilities(person_window: bool) -> tuple[ClientCapability, ...]`
     drops `PERSON_STEPS_V1` when the window is unavailable or disabled.
   - `MessageType.PERSON_STEP_STATE = "person_step_state"`
     (server→runner). Its payload is `steps`, a list of 0 to 5 open steps
     with the newest request first. Each step is a
     `PersonStepStatePayload` dataclass with these fields:
     `person_step_id: int`, `round: int`, `status`
     ("waiting"/"not_confirmed"), `run_name`, `case_title`,
     `step_label` ("Step N of M"), `kind`
     ("interactive"/"command"/"restore"), `instruction`, `expected_result`,
     `command`, `shell_label`, `objection`, `blockers: tuple[str, ...]`,
     `case_url` (empty when `SITE_URL` is unset). An empty list means
     "nothing for you; hide".
   - `MessageType.PERSON_STEP_ACTION = "person_step_action"` (runner→server):
     `action` ("report"/"overrule"/"fail"/"block"), `person_step_id`, `round`,
     `outcome` ("as_expected"/"different", report only), `note`, `output`.
   - `MessageType.PERSON_STEP_ACTION_RESULT = "person_step_action_result"`
     (server→runner, echoes the action's `request_id`): `ok: bool`,
     `message: str` (user-safe), `queued: bool`.
   - Parse and build helpers for these three messages, typed like the
     existing `parse_*_payload`, with length caps checked at parse time:
     note ≤ 2000, output ≤ 20000 (the server's limits are restated as
     `Final` constants here, since the runner cannot import models).
2. **`projects/models.py` and migration**: as above.
3. **`projects/person_step_actor.py`** (new leaf): frozen dataclass
   `PersonStepActor(user: CustomUser | None, channel: PersonStepChannel)` with
   `web(user)` and `runner()` constructors and `label() -> str`. The label is
   `person_label(user)` for web and "a person at the Test Runner" for the
   runner. It never includes the runner-reported hostname, which is untrusted
   text that would reach the LLM, the result text and TestRail.

### Services
**`projects/person_step_services.py`** (changed; the logic stays here):
- The four actions take `actor: PersonStepActor` instead of `user`. They write
  `reported_by=actor.user` / `closed_by=actor.user` (None for the runner),
  `reported_via`/`closed_via=actor.channel`, `reporter=actor.label()`, and
  the verdict text "Marked failed by {actor.label()} …".
- New keyword `expected_round: int | None = None`. When it is given,
  `_lock_step` refuses a step whose `round` differs. This is the stale-answer
  guard for a runner answer to round 1 that arrives after the step was
  reopened "not confirmed" as round 2 (same id). The web passes None, so its
  behaviour is unchanged.
- `overrule_person_step` sets `closed_via`. `_STEP_GONE` becomes
  channel-neutral: "This step was already answered or is no longer waiting."
- `_checkpoint_person` rebuilds `reporter` from the row with a helper that
  understands `reported_via == runner`.
- `PersonStepPanel` gains `runner_can_answer: bool`. It is true when the
  project's runner is connected with `PERSON_STEPS_V1`, computed from the
  `project` already loaded (no query). The card uses it for a hint.

**`projects/person_step_rows.py::_settle_row`**: the OVERRULED branch becomes
`Q(closed_by__isnull=False) | ~Q(closed_via="")`. A runner overrule has no
user, so `closed_via` is what marks it.

**`projects/runner_person_step_services.py`** (new, one use case: "the
project's Test Runner reads and answers its open person steps"):
- `get_runner_person_steps(project_id: int) -> tuple[RunnerPersonStepView, ...]`
  reads the actionable steps of the project's PAUSED runs. It runs one query:
  `CasePersonStep` filtered by `test_run_test_case__test_run__project_id`,
  run status PAUSED, case status `awaiting_person` and step status in
  (`waiting`, `not_confirmed`). It uses
  `select_related("test_run_test_case__test_case", "test_run_test_case__test_run")`,
  `.only(...)` (no `logs`, no checkpoint), and annotates the plan size with
  `jsonb_array_length(execution_checkpoint->'plan')` so the checkpoint JSON is
  never loaded. It orders by newest first, limit 5. When rows exist, it runs
  `list_reopen_blockers` once per distinct run (normally one). It also builds
  `case_url` from `SITE_URL` and `reverse("projects:test_run_case_detail")`.
  `RunnerPersonStepView` is a frozen dataclass with a `to_payload()` that
  matches `PersonStepStatePayload`.
- `answer_from_runner(*, project_id: int, channel_name: str, action: RunnerPersonStepAnswer) -> PersonStepActionResult`
  runs these checks in order:
  1. The connection still owns the project
     (`Project.agent_channel_name == channel_name`); otherwise
     `PersonStepError`. A superseded or force-disconnected socket cannot act.
  2. A per-project throttle in the Django cache, 20 answers per 60 s, like
     `project_member_services._check_invite_throttle`; otherwise
     `PersonStepError("Too many answers…")`.
  3. Resolve the step to its pivot within the project, using one query
     scoped by `test_run_test_case__test_run__project_id=project_id`. A step
     of another project is indistinguishable from a missing one.
  4. Call the matching `person_step_services` function with
     `actor=PersonStepActor.runner()` and `expected_round=action.round`.

  `RunnerPersonStepAnswer` is a frozen dataclass built by the consumer from
  the validated form.
- No other business logic; all rules stay in `person_step_services`.

**`projects/runner_person_step_push.py`** (new leaf, no model imports):
`nudge_runner_person_steps(project_id: int) -> None` sends the
payload-free event `{"type": "controller.person_steps_changed"}` to
`controller_<id>`. It never raises (it logs). This mirrors
`notifications.services.inbox_broadcast`: nothing secret goes onto the channel
layer, and the consumer reads the state itself.

**Where the nudge is called:** `projects/services.py::broadcast_run_and_pivots`
and `broadcast_test_run_status` (one extra `group_send` each). Every person
step transition the window cares about changes the run status or goes through
one of these two: pause (open or not confirmed), the four actions, hand-back,
mode switch, abort, help Mark failed/blocked cancelling an open step, and run
finish. The implementer greps every `PersonStepStatus.` write and every
`CasePersonStep.objects…update(` to confirm that each is followed by one of
the two broadcasts. Any that is not gets an explicit
`transaction.on_commit(lambda: nudge_runner_person_steps(project_id))`.

### Forms
`projects/forms.py`: `RunnerPersonStepActionForm(forms.Form)` with these
fields: `action` (ChoiceField report/overrule/fail/block), `person_step_id`
(min 1), `round` (min 1), `outcome` (`PersonStepOutcome`, required only for
report), `note` (max `HELP_NOTE_MAX_LENGTH`), `output` (max
`PERSON_STEP_OUTPUT_MAX_LENGTH`, strip False). Its `clean()` repeats the "note
required when different" rule. Over-long input is rejected, not truncated,
like the web forms.

### Pages and forms
No new URL. The web views change only to pass `PersonStepActor.web(request.user)`.

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `projects:test_run_case_person_{report,overrule,fail,block}` (unchanged) | POST | `@project_membership_required` | unchanged | `_person_step_card.html`: new `person_step_panel.runner_can_answer` gives the hint "You can also answer this on the Test Runner window." The confirming view shows "Reported at the Test Runner" when `step.reported_via == "runner"`. | unchanged; `_STEP_GONE` text neutral |

### Background work and WebSockets
**Consumer** (`projects/controller_consumer.py`, stays thin):
- Store `self._capabilities` from the handshake. Store
  `self._person_steps_sent: tuple[tuple[int, int, str, tuple[str, ...]], ...] | None`,
  a fingerprint of the last snapshot (id, round, status, blockers) used to
  drop duplicates.
- After the `ok` ack, if `PERSON_STEPS_V1` is advertised: read
  `get_runner_person_steps` (via `sync_to_async`) and send
  `person_step_state`. This covers reconnects and a runner restart mid-step.
- `controller_person_steps_changed(event)`: if capable, re-read and send only
  when the fingerprint changed. An empty list is sent once after a non-empty
  one.
- `_route_authenticated_message` gets a `person_step_action` handler. It
  bounds `len(text_data) <= 256 KiB` for this type, validates with
  `RunnerPersonStepActionForm`, calls `answer_from_runner` with
  `self.channel_name`, maps `PersonStepError` to `ok=False`, and sends
  `person_step_action_result` with the same `request_id`. On success it
  sends `ok=True` with "Reported. The agent is checking it." (and the other
  web flash texts), or with the blocker text when `queued` is False. A
  runner that did not advertise the capability gets `ok=False` with
  "unsupported". Unexpected exceptions are logged and answered with a
  generic error. The socket is never dropped over this.
- No new Celery task and no queue change. The answer runs in the consumer's
  `sync_to_async` thread, like the existing handshake DB work. It holds short
  row locks and then `queue_pending_cases` (a `send_task`), the same work the
  web view does in a request.

**Runner side** (new modules, own strict mypy config, win32 pass):
- `controller_client/person_window_ipc.py`: the stdlib-only JSON-lines
  messages between the runner and its window process:
  - runner→window: `show(steps, connected)`, `hide`, `result(ok, message)`,
    `connection(connected)`, `quit`;
  - window→runner: `ready`, `hidden` (ack), `action(...)`, `closed_by_user`.
- `controller_client/person_window_app.py`: the Tk program, run as
  `python -m controller_client.person_window_app` by the runner's own
  `sys.executable`. Tk owns that process's main thread, which satisfies the
  macOS AppKit rule. A stdin reader thread feeds a `queue.Queue`, polled with
  `after(50)`. The window shows:
  - the project and run name, the case title and "Step N of M · attempt R";
  - the instruction, the expected result, and the agent's objection when
    `not_confirmed`;
  - for command steps, the command in a read-only `Text` with **Copy**
    (`clipboard_clear`/`clipboard_append`) and the shell label;
  - the outcome radios, Note, and Output (command steps only);
  - **Report / Report again**, **Overrule** (only when `not_confirmed`),
    **Mark failed** and **Mark blocked**, with a confirm dialog for the last
    two;
  - "N more steps waiting" with a picker when there are several (several
    paused runs), the blockers as a warning line, and `case_url` as
    selectable text with a Copy button.

  Buttons are disabled while an answer is in flight or the runner is
  disconnected ("Reconnecting…"). All text is plain Tk text, so there is no
  markup to inject. The window raises once per new step or round, is never
  `-topmost`, does not grab focus, and keeps its position for the session.
  Closing it with the window manager only hides it until the next step.
- `controller_client/person_window.py`: the asyncio-side `PersonWindow`
  manager. It starts the subprocess lazily on the first non-empty state,
  using `asyncio.create_subprocess_exec` (the Proactor loop on Windows is the
  `asyncio.new_event_loop()` default). It restarts the subprocess at most 3
  times per 10 minutes, then gives up and logs "answer on the web". It
  exposes:
  - `apply_state(steps)`;
  - `ensure_hidden()`, which awaits the `hidden` ack (≤ 1.5 s) plus a
    150 ms compositor settle and is a no-op without IPC when already hidden;
  - `maybe_reshow()`;
  - `set_connected(bool)`;
  - `actions()`, an async iterator of validated `PersonStepActionPayload`;
  - `stop()`.

  It keeps the window process's pid so the close policy can protect it. It
  also provides `probe() -> bool`, which imports tkinter and creates and
  destroys a `Tk()` in a subprocess with a 10 s timeout, run once at startup.
- `controller_client/client.py`:
  - Compute the advertised capabilities once at start from
    `CONTROLLER_PERSON_WINDOW` and `PersonWindow.probe()`, and send them in
    `_send_handshake` instead of `CLIENT_CAPABILITIES`.
  - Add a handler for `PERSON_STEP_STATE` that calls
    `window.apply_state(...)` and returns immediately (the message loop is
    serial). Add a handler for `PERSON_STEP_ACTION_RESULT` that calls
    `window.result(...)`.
  - A background task forwards `window.actions()` to the socket with
    `_send_message` (a new `request_id` per answer, with a 30 s
    result timeout that re-enables the buttons with "No answer from the
    server; check Punk Hazard").
  - **Hide before every action:** `_dispatch` awaits `window.ensure_hidden()`
    before any handler other than `PING`, `PERSON_STEP_STATE` and
    `PERSON_STEP_ACTION_RESULT`.
  - After sending an answer the window hides itself at once. The confirm
    agent's first screenshot comes after the report, so it is already gone.
  - `maybe_reshow()` runs 5 s after the last action when the latest state
    still has an open step, for example after a help-chat machine action or
    while another run is executing.
  - On disconnect, call `window.set_connected(False)`. `stop()` stops the
    window.
- `controller_client/app_close_policy.py`: `RunnerProcesses` gains
  `helper_pids: frozenset[int]` (the window process). `is_runner()` covers
  them, so `close_app("Python")` can never kill the window. This applies to
  macOS, where it shows as a "Python" app, and to Windows.
- `controller_client/config.py` and `controller_client/example.env`: add
  `CONTROLLER_PERSON_WINDOW` (bool, default `true`) and the `--no-person-window`
  CLI flag. The server's `_generate_env_content` adds the line too.

### Settings / infrastructure
- Server: no new setting; the throttle values are module constants.
  `SITE_URL` is reused.
- Runner: `CONTROLLER_PERSON_WINDOW`. The setup scripts check `import
  tkinter` in the venv and print how to get it. They never fail on it:
  - Ubuntu: `sudo apt-get install -y python3-tk` (setup.sh installs it with
    the GStreamer packages on apt systems);
  - Homebrew macOS: `brew install python-tk@3.13`;
  - Windows: re-run the python.org installer with "tcl/tk and IDLE".
- Daphne size flags are unchanged (the messages are small).

### Data flow
1. The worker pauses for a person, through `_pause_run_for_person`. On
   commit, `broadcast_run_and_pivots` calls `nudge_runner_person_steps(project)`.
2. The controller consumer (current owner only, via the group) hears
   `controller.person_steps_changed`. It reads `get_runner_person_steps`,
   finds a new fingerprint and sends `person_step_state{steps:[…]}`.
3. The runner applies the state. The window process starts if needed, shows
   the step and raises once.
4. The person performs the step on the machine and presses Report. The
   window sends `action` over stdin/stdout to the runner. The window hides
   itself. The runner sends `person_step_action`.
5. The consumer validates the form and calls `answer_from_runner` (owner,
   throttle, project scope, then `report_person_step(actor=runner,
   expected_round=r)`). That locks, writes the step (`reported_via=runner`)
   and the checkpoint, and queues the cases. On commit it broadcasts: the
   web case page reloads and shows "Reported at the Test Runner", and the
   nudge leads to an empty or "confirming" state, so the runner stays
   hidden. The consumer replies `person_step_action_result{ok, queued}`.
6. The worker resumes and the confirm agent takes screenshots. The runner
   sees `screenshot_request` and `ensure_hidden()` is a no-op, so the window
   is not in the capture or the vision parse.
7. If the agent cannot confirm, the step is paused again (`not_confirmed`,
   round + 1). The nudge brings the window back with the objection.
8. If the web answers first, the step closes, the nudge empties the runner
   list and the window hides. A runner answer in flight gets "This step was
   already answered or is no longer waiting."

## Alternatives considered
- **Terminal prompts in the runner console.** No dependency, but the console
  is busy with logs and reading stdin from asyncio on Windows needs a thread.
  There is no Copy button and multi-line output is awkward. Rejected as the
  primary UI; possible as a later fallback (Q2).
- **A local web page served by the runner on 127.0.0.1.** Rich rendering,
  but the runner cannot hide a browser window during agent actions, so it
  would appear in screenshots. It adds a localhost attack surface (CSRF and
  DNS rebinding from the very sites under test). Opening Punk Hazard itself
  in a browser on the test machine already gives the same result.
- **OS notifications or a tray icon.** They cannot take a multi-line report.
  A tray icon needs pystray/pyobjc/AppIndicator, which is unavailable on
  stock GNOME. Not chosen.
- **Tk inside the runner process (thread).** It crashes on macOS (AppKit is
  main-thread only), and the runner's main thread runs asyncio. The
  subprocess also isolates Tk crashes from the runner.
- **Pushing deltas (`offered/updated/closed`) instead of snapshots.**
  Snapshots are idempotent: reconnects, duplicate nudges and both-sides races
  all converge on the latest state without ordering logic.
- **Putting the step on the channel layer event.** A payload-free nudge plus a
  read in the consumer keeps case text off Redis (the inbox convention) and
  gives one code path for nudge and reconnect.
- **A per-project "allow answers from the runner" toggle.** Not added. The
  Connection key already gives full control of the machine and is the source
  of every screenshot and command output the verdicts rest on, so answering
  steps grants no real new power. HITL is chosen per run by a member. The
  runner-side `CONTROLLER_PERSON_WINDOW=false` lets the machine owner opt out.

## Risks and mitigations
- **The window in the agent's screenshots or vision parse.** Mitigations:
  hide on answer, hide-before-every-action with an ack, a settle delay, no
  `-topmost`, and re-show only after 5 s idle. If the hide ack times out,
  the action still runs, with a warning logged. Verify on all three OSes
  (compositor animation on macOS/GNOME).
- **The window covering the app under test while the person works.** It is
  small, movable, not on top and keeps its position. The person controls it.
- **tkinter missing or no display** (Homebrew Python, Ubuntu without
  python3-tk, a session without `DISPLAY`/XWayland). The probe fails, the
  capability is not advertised, and the web card works as today. The runner
  logs one clear line and `diagnose --window` explains it.
- **XWayland quirks on GNOME** (blurry on HiDPI, no native decorations).
  These are cosmetic. The GNOME ScreenCast stream captures XWayland windows
  too, so hiding matters there as well.
- **Auth and audit.** Answers come only from the connection that currently
  owns the project. Steps are scoped by project in the query. There is a
  round guard and a throttle. Identity is recorded as channel `runner` with
  no user, and the label "a person at the Test Runner". Runner-reported
  hostnames are never used in labels or prompts.
- **Missed nudge leaves a stale window.** The snapshot is re-read on every
  run broadcast and on reconnect. A stale answer is refused by the service
  lock, status and round, and the refusal triggers a re-read.
- **Overrule close reason.** Fixed by `closed_via` in `_settle_row`. Without
  it, a runner overrule would be recorded as "confirmed".
- **Consumer thread work.** `answer_from_runner` runs in the
  consumer's sync thread pool, sharing it with action replies. It is short,
  with the same locks as the web view. The throttle bounds abuse.
- **XSS.** New template text is autoescaped and there is no `|safe`. The Tk
  window renders plain text.
- **Migration.** Additive columns with defaults, no backfill. Rollback: drop
  the columns, though runner-made reports then show no reporter.
- **Shared touch points with `feat-runner-self-update`:**
  - `controller_client/protocol.py`: `CLIENT_VERSION`, new `ClientCapability`
    and `MessageType` members, and the move from
    `CLIENT_CAPABILITIES` to runtime `advertised_capabilities`;
  - `client.py`: the handler table and `_send_handshake`;
  - `controller_consumer.py`: `_route_authenticated_message` and
    post-handshake work;
  - `config.py`/`example.env`/`_generate_env_content`, the setup scripts and
    `diagnose.py`.

  Whichever lands second rebases and takes the next `CLIENT_VERSION` minor.
  A self-update that restarts the runner must `stop()` the window process
  first. On Windows the venv's `python.exe` is locked while the window
  process runs.

## Implementation steps
1. **Shared code (orchestrator, before sub-agents).**
   - `controller_client/protocol.py`: messages, capability,
     `advertised_capabilities`, payloads and parsers, version.
   - `projects/models.py` + `projects/migrations/0039_person_step_channel.py`.
   - `projects/person_step_actor.py`.
   - Run `pre-commit` on these files.
2. **Part A, person step services** (files: `projects/person_step_services.py`,
   `projects/person_step_rows.py`, `projects/person_step_views.py`).
   - Actor parameter and `expected_round`; `reported_via`/`closed_via`;
     neutral `_STEP_GONE`.
   - `PersonStepPanel.runner_can_answer`; the `_settle_row` OVERRULED
     condition.
   - Views pass `PersonStepActor.web(request.user)`.
   - Exposes the signatures used by Part B.
3. **Part B1, runner services** (files: `projects/runner_person_step_services.py`
   (new), `projects/runner_person_step_push.py` (new), `projects/forms.py`):
   - `get_runner_person_steps`, `answer_from_runner`,
     `RunnerPersonStepView`, `RunnerPersonStepAnswer`;
   - `nudge_runner_person_steps`;
   - `RunnerPersonStepActionForm`.
4. **Part B2, consumer and nudge sites** (files:
   `projects/controller_consumer.py`, `projects/services.py`, plus
   `on_commit` nudges wherever the grep in Services finds a gap):
   - capabilities kept on the consumer, the post-handshake snapshot, the
     `controller_person_steps_changed` handler, the `person_step_action`
     handler;
   - the nudge in `broadcast_run_and_pivots`/`broadcast_test_run_status`;
   - `_generate_env_content` adds `CONTROLLER_PERSON_WINDOW=true`.

   Consumes B1's functions by name (signatures fixed in this plan).
5. **Part C1, window process** (files:
   `controller_client/person_window_ipc.py`,
   `controller_client/person_window_app.py`,
   `controller_client/person_window.py`): Tk app, IPC, manager, probe.
6. **Part C2, runner integration** (files: `controller_client/client.py`,
   `controller_client/config.py`, `controller_client/example.env`,
   `controller_client/main.py`, `controller_client/app_close_policy.py`):
   - capabilities from config and probe; handlers; the action forwarder
     task;
   - hide-before-action in `_dispatch`; reshow timer; connection state;
     `helper_pids`.

   Consumes the C1 manager API listed under Design.
7. **Part D, runner diagnostics and setup** (files:
   `controller_client/person_window_diagnostics.py` (new),
   `controller_client/diagnose.py`, `controller_client/scripts/setup.sh`,
   `setup.ps1`, `setup.bat`):
   - `diagnose --window` checks the tkinter import, Tk version, display and
     XWayland on Linux. It shows a sample step, asks "Do you see the
     window?", then hides it, takes one screenshot with the desktop
     backend and confirms nothing was left behind (interactive;
     `--no-interactive` runs the automatic checks only).
   - Setup scripts warn about tkinter as listed under Settings.
8. **Part E, web card** (files: `templates/projects/includes/_person_step_card.html`,
   `static/css/punk-hazard.css` if a class is needed): the runner hint and
   "Reported at the Test Runner".
9. **Integration (orchestrator).**
   - Wire the seams and run the nudge-site grep.
   - Update `CLAUDE.md` (controller_client paragraph: `PERSON_STEPS_V1`,
     the window process, hide-before-action; the HITL paragraph), and
     `controller_client/README` if one exists.
   - Run `pre-commit run --all-files` (server mypy plus the
     controller_client mypy, including the win32 pass),
     `python manage.py check` and `makemigrations --check`.

Parts A, B1, C1, D and E can run in parallel after step 1. B2 starts once B1's
signatures are in place (they are fixed above, so B2 can run in parallel
against them). C2 runs in parallel against C1's manager API.

## Verification
No automated tests (project rule). Manual checks:
1. `pre-commit run --all-files`; `controller_client/.venv/bin/mypy --config-file controller_client/setup.cfg controller_client/` (and `--platform win32`); `python manage.py check`; `makemigrations --check`.
2. Old runner (0.7.0) against the new server: it connects, no
   `person_step_state` is ever sent and the web card works. New runner against
   an old server: no window ever appears.
3. `diagnose --window` on macOS (python.org and Homebrew without python-tk),
   Windows 11, and Ubuntu 26.04 GNOME Wayland (with and without python3-tk).
4. A HITL run with one interactive step and one command step:
   - the window appears with the right text;
   - Copy puts the command on the clipboard;
   - Report hides the window, and the confirm agent's screenshots
     (case page screenshot list) do not show it;
   - the web card reloads and says "Reported at the Test Runner".
5. A not-confirmed round: the window comes back with the objection and
   "attempt 2". A stale round-1 answer (send it by hand from a scratch
   client) is refused. Overrule from the runner records close reason
   `overruled` (`manage.py shell -c` query).
6. Mark failed and Mark blocked from the runner: the verdict text says "by a
   person at the Test Runner", Blocked keeps `help_skipped`, and
   `grep "status = TestRunTestCaseStatus.BLOCKED"` still has one hit.
7. Race: open the web card, answer on the runner, then submit the web form.
   The web flashes the neutral "already answered" text. Then the reverse
   order.
8. Reconnect: kill the runner's network or restart the runner mid-step. The
   window shows "Reconnecting…" and the step comes back after the
   handshake. Force disconnect from the panel: the window hides and answers
   are refused.
9. Two paused HITL runs in one project: the picker shows both. With
   another run STARTED, the answer saves and the blocker text appears in
   the window.
10. Mode switch away from HITL while the window shows a step: the window
    hides (handed back).
11. `close_app` "Python" on macOS while the window is up: refused as the
    runner's own process.
12. Throttle: 21 answers in a minute (scripted) are refused with the
    throttle message.
13. Run the `reviewer` and `verifier` agents (give the verifier the request
    and this plan).

## Open questions
- **Q1. v1 scope.** Recommended: HITL person steps only (instruction,
  command with Copy, report as expected or different with note and output,
  overrule, Mark failed, Mark blocked). Need help requests (Continue / Mark
  failed / Mark blocked + note) are a natural v2 on the same snapshot and
  answer mechanism (a `help_request` item kind). Help chat and result chat
  (multi-turn, markdown, streaming, proposal cards) stay in the web UI. The
  window links to the case page instead. *Assumption in this draft: person
  steps only.*
- **Q2. Runner UI.** Recommended: a small Tk window in a separate process
  started by the runner. It is stdlib, works on all three OSes (XWayland on
  GNOME), satisfies the macOS main-thread rule, and the runner can hide it
  before agent actions. Alternatives: terminal prompts in the runner console,
  a localhost web page, or OS notifications only. When tkinter is missing:
  recommended is no window (web only, with a clear log and diagnose hint)
  rather than also building a terminal-prompt fallback. *Assumption: Tk
  subprocess, no fallback UI.*
- **Q3. Identity of runner answers.** The runner authenticates with the
  project's Connection key, not a user. Recommended: record channel `runner`
  with no user, labelled "a person at the Test Runner", in verdict text,
  checkpoint `reporter` and summaries. The alternative is a pairing step: a
  member clicks "Answer as me on this Test Runner" in the web UI, gets a
  short code and types it in the window, which binds that connection to the
  user so `reported_by`/`closed_by` name them. *Assumption: anonymous runner
  channel.*
- **Q4. Which actions the runner may take.** Recommended: all four, like the
  web card. The key already controls the machine and supplies the evidence
  the verdicts rest on, and HITL is chosen per run by a member. Stricter
  option: Report and Mark failed only, with Overrule and Mark blocked
  web-only. *Assumption: all four.*
- **Q5. Window behaviour.**
  - Recommended: raise once per new step or round, never always-on-top,
    no focus grab, and hide on answer and before every agent action, with
    re-show after 5 s of quiet.
  - Alternatively: always-on-top while a step is open (more visible, but it
    covers the app under test).

  *Assumption: the recommended behaviour.*
- **Q6. Default on or off.** Recommended: `CONTROLLER_PERSON_WINDOW=true` by
  default, so a fresh runner offers the window. Unattended Night machines
  never see it, because steps exist only in HITL runs. Alternative: opt-in.
  *Assumption: on by default.*
- **Q7. Version coordination with "Test Runner self-update".** Both plans
  bump `CLIENT_VERSION` and add capabilities in `controller_client/protocol.py`.
  Recommended: land self-update first if it is ready, since an auto-updating
  runner makes this feature reach machines without a manual ZIP download.
  The second plan takes the next minor version. *Assumption: this plan uses
  0.8.0 and renumbers if needed.*

## Changelog
- 2026-10-28: created (Draft).
- 2026-10-28: user answers. Q2: a native window in its own process (as planned), closed/hidden automatically before every screenshot and agent action. Q3: the Test Runner asks for the person's email when it starts; the server records that member as reporter (see the identity caveat raised to the user). Other questions still open.
- 2026-10-28: user answers. Q3 refined: the email entered at Test Runner start is enough (no one-time code); the server checks it is an active project member, otherwise reports are labelled "a person at the Test Runner"; the email is stored in the runner's .env and carried over by self-update; reports record that member with "via Test Runner". Q1, Q4–Q7: the recommendations as written. Status In progress; build after feat-runner-self-update.
