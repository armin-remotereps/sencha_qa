# feat: Pause a running case and discuss it with the agent

Status: In progress
Created: 2026-10-31
Request: While a test case is running, a person must be able to pause it and go
straight to a discussion with the agent about that case. In the discussion they
talk it through, change the test flow (redesign the plan) if needed, and have the
agent do things on the test machine. Then they decide how the case goes on:
- continue from the current screen;
- restore the earlier steps first;
- start the case again;
- continue with a redesigned plan;
- mark it failed, blocked or for re-test.

This works in every run mode (Attended, Night, Human in the loop), also while a
person step waits, from the web case page and (through a seam described here) from
the Test Runner window. The machine is left exactly as it was when the case
stopped.

The pause dialog takes an optional first message for the agent. Once the case has
really stopped, the server opens the discussion and sends that message. The
continue mode choice (and an agent recommendation of a mode) is extended to every
help request.

## Context

- **Need help**:
  - `projects/services.py::_pause_run_for_help` opens a `CaseHelpRequest`, sets
    the case `NEED_HELP` and the run `PAUSED`, skips teardown and cleanup, and
    then notifies or enqueues the Night auto-fix (`send_task` by name,
    `machine_setup/task_names.py`).
  - `_pause_case_after_fault` decides `continue_mode` with
    `projects/case_faults.py::help_continue_mode`: no checkpoint means
    `from_scratch`; a runner fault or an unfinished restore means `restore`;
    anything else means `current_screen`.
  - `case_help_services.restore_open_help_after_runner_update` switches open
    current-screen requests to `restore` after a runner update.
- **Help chat** (`docs/changed/2026-10-24-help-chat.md`):
  - `machine_setup/services/open_help_chat.py::open_help_chat(project, pivot,
    user, text)` creates or reuses the request's one `case_help` thread and
    sends `text` (an empty text on a new thread sends `DEFAULT_FIRST_MESSAGE`).
    It raises `HelpChatSendRefusedError` (the thread is kept) when the send is
    refused.
  - The tools are in `machine_setup/services/help_chat_tools.py`:
    `get_help_case`, `propose_plan_change` and `propose_resolution(action,
    note)`. `propose_resolution` is stored by
    `projects/case_help_proposal_services.record_agent_proposal` and cleared on
    every resolution.
  - Continue, Mark failed, Mark blocked and Mark for re-test are in
    `projects/case_help_services.py`. Their `_resolve` already calls
    `record_case_activity` and `credit_cases`.
  - `_write_help_key` turns `continue_mode` into the checkpoint help key. With
    `current_screen` it writes `continue_pending`, keeps the person key and
    skips cleanup and restore. With `restore` there is no `continue_pending`,
    so cleanup and restore run. With `from_scratch` it writes `{}`.
- **Stop at the next check.** `projects/services.py::_build_cancellation_check`:
  - It is polled before each step, before every LLM and tool call in
    `agents/services/agent_loop.py`, inside the retrying controller tools, and
    in `wait_for_ready_worker`. `_build_verdict` runs without a check.
  - A Needs re-test mark already uses it per case.
  - It raises `AgentCancelledError`, which `execute_test_run_test_case` turns
    into `_cancel_pivot_if_in_progress`.
  - The orchestrator moves the cursor to a step before running it, so an
    interrupted step re-runs.
- **Interrupted steps in the agent layer.** A non-empty `CheckpointHelp.fault`
  makes `_interrupted_by_fault` / `_applied_help` accept a step with no failed
  attempt, and `format_help_state_line` says the step may be partly done.
- **Human in the loop.**
  - `AWAITING_PERSON` keeps the run PAUSED with an open `CasePersonStep`.
  - A `reported` step is confirmed in the worker while the case is IN_PROGRESS.
  - Runner answers arrive as `person_step_action` from the owner connection
    (`projects/runner_person_step_services.answer_from_runner`). They are
    throttled at 20/min/project, and the actor comes from
    `projects/person_step_actor.PersonStepActor`: a member's email counts, a
    read-only member or an unknown email is anonymous.
- **Generations.**
  - Every Start, Resume and wake (`run_reopen_services.reopen_run`) bumps
    `TestRun.execution_generation`.
  - `execute_test_run_case` is `acks_late` + `reject_on_worker_lost`, so a
    redelivered task keeps its generation.
- **Lock order**: project, then run, then help request, then open person steps,
  then case. Worker writes lock only the case row.
- **Night auto-fix.**
  - `report_case_fix(fixed, summary)` ends in
    `case_help_services.finish_case_auto_fix`, which continues with the stored
    `continue_mode`.
  - A switch to Night auto-fixes an open request without a thread
    (`run_help_mode_services._request_waiting_for_person`).
- `docs/plan/feat-tester-console.md` is being rewritten as a Test Runner (Tk)
  window feature. It consumes the contract in "Contract for the Test Runner
  window".

## Goals
- Add a Pause button on a running case and on a case whose person step waits.
  It is available to writable members on the web and, through the runner seam,
  at the Test Runner.
- The worker stops at the agent's next action (an LLM or tool call, a step
  boundary, or just before the verdict) and keeps the checkpoint. The case
  becomes Need help with a request "Paused by <person> at step N". There is no
  teardown and no cleanup.
- An optional first message from the Pause dialog is stored on the server.
  Once the case has stopped, the server opens the `case_help` thread and sends
  the message as the person's first turn, even if every page is closed. An open
  case page navigates to the discussion live.
- On Continue, the person picks the continue mode for every help request,
  limited to modes that are safe for the request. The help-chat agent may
  recommend a mode, which is shown but not pre-selected.
- Keep a stable service, panel and URL contract, plus a runner action seam.

## Non-goals
- No new case or run status, and no run-level pause (Q1).
- No "pause after the current step" (Q2), and no cancelling a pending pause
  (Q8).
- No ntfy for a pause; the inbox and attention listing is enough (Q6). No
  credit, session activity or web throttle for the pause itself (Q7).
- The Test Runner window UI, its state snapshot and its chat are not designed
  here; they belong to `docs/plan/feat-tester-console.md`. This plan defines
  only the server-side seam for a runner pause.
- No mode choice for the Night auto-fix agent (see Design, Continue mode).

## Design

### Data model
`projects/models.py`, one migration `projects.0050_case_pause`. All fields are
nullable or have a blank default, so it is additive with no backfill.

- `TestRunTestCase`: the pause request (at most one; overwritten).
  - `pause_requested_generation`: `PositiveIntegerField(null=True)`. The request
    counts only for that run generation.
  - `pause_requested_by`: FK user, `SET_NULL`, `null=True`, `related_name="+"`,
    `db_index=False`. Null means an anonymous Test Runner person.
  - `pause_requested_label`: CharField(255), server-written. It is the actor
    label, for example "Ana via Test Runner".
  - `pause_requested_via`: `PersonStepChannel` (web or runner).
  - `pause_requested_at`: DateTimeField(null=True).
  - `pause_message`: TextField, `max_length=PAUSE_MESSAGE_MAX_LENGTH` (8000, kept
    equal to `machine_setup.forms.SETUP_MESSAGE_MAX_LENGTH` with a comment;
    `projects` cannot import `machine_setup.forms`), blank. It is the first
    message for the agent, and it survives the worker stopping because it is in
    the database row, not in the page or the task args.
  - `PAUSE_REQUEST_FIELDS` / `PAUSE_REQUEST_CLEARED` live next to
    `RETEST_CLEARED`.
- `CaseHelpRequest`:
  - `stopped_by_pause`: BooleanField(default=False). True means a person's pause
    stopped the case: `fault` is `""`, and `reason` and `ask` are server texts.
    The property `is_person_pause` returns it.
  - `paused_by` (FK user, SET_NULL, null), `paused_by_label` (CharField 255,
    blank) and `paused_via` (`PersonStepChannel`): who asked to pause. These are
    also set when a pause was pending but the agent's `need_help` or a fault
    stopped the case first; the person still gets the discussion (see Data
    flow). The property `has_pending_person` returns `paused_by_label != ""`.
  - `pause_message`: TextField (same cap, blank), copied from the case when the
    request opens.
  - `pause_message_state`: CharField with the new `PauseMessageState`:
    `""` (none), `pending`, `sent`, `refused` or `dropped`.
  - `pause_message_note`: CharField(500), a user-safe reason when the state is
    `refused`.
  - `agent_proposal_continue_mode`: CharField(16), `HelpContinueMode` choices,
    blank. It is the mode the help-chat agent recommends. It is cleared with
    the rest of the proposal.
- `PersonStepClose.PAUSED = "paused", "Paused for a discussion"` (choices only).
- No new `BlockedReason`. A pause is not a fault. `BlockedReason` drives
  auto-fix and fault texts.

### Agent layer (ORM-free, small)
- `agents/types.py::CheckpointHelp` gains `paused_by: str = ""` (a label; it is
  set only when `stopped_by_pause`). `agents/checkpoint.py` reads and writes it
  as an optional key. The version is unchanged.
- `agents/services/orchestrator.py`: `_interrupted_by_fault` and
  `_applied_help` treat `help.paused_by` like `help.fault`.
- `agents/services/orchestrator_prompts.py`: `format_help_state_line` and
  `help_verdict_note` get pause wording: "Step N was paused by a person and
  discussed; the step may be partly done. Check the screen." The person's note
  and the summary come through `_help_details` as today. The label is a member
  name or a fixed runner label, never runner-reported text.
- `orchestrator._execute_sub_tasks` gets one `_check_cancelled` before
  `progress.set_phase("verdict")`. Abort and re-test discard the verdict anyway,
  and a resume at the verdict phase re-runs the verdict.

### Continue mode for every help request (Q5)
- In `projects/case_pause_policy.py` (a pure leaf), add
  `allowed_continue_modes(stored: HelpContinueMode, fault: str,
  checkpoint_json: object) -> tuple[HelpContinueMode, ...]`. It returns the
  modes in display order, stored mode first:
  - **from_scratch: always.** It clears the checkpoint and runs cleanup first.
    This is always safe, at the cost of redoing the case.
  - **restore**: when a checkpoint exists. It needs `1 < next_step <=
    len(plan)` unless it is the stored mode. Runner faults store restore even at
    step 1, where it means cleanup and then step 1.
  - **current_screen**: when a checkpoint exists and the stored mode is not
    `restore`. A stored `restore` means the screen cannot be trusted:
    - a runner fault (`runner_disconnected`, `runner_unresponsive`,
      `runner_outdated`): the runner may have restarted, and its handover
      closes the apps it opened;
    - a fault before a due restore finished: the earlier steps' state was never
      brought back;
    - a runner update after the pause (`restore_open_help_after_runner_update`).

    Re-running the cursor step on that screen would test the wrong state and can
    pass or fail for reasons unrelated to the application. A person who checked
    the machine can still restore, which is cheap, or start again. So
    current_screen is withheld rather than allowed with a warning.
  - **No checkpoint** (the stored mode is from_scratch): only from_scratch.
- `continue_after_help(..., continue_mode: HelpContinueMode | None = None)`.
  - **Validation.** Under the locks the mode must be in
    `allowed_continue_modes(...)`, otherwise `RunHelpError` with a user-safe
    message. None means the stored mode.
  - **Redesign.** With `use_plan_revision` and a draft, the mode must be
    `current_screen`. A redesign continues at C+1 from the current screen, so
    restore or from_scratch with a redesign are refused. When the stored mode
    is `restore`, a help redesign is refused too, with "Restore first: the
    screen cannot be trusted". This closes an existing gap, because today a
    redesign on a restore request skips the restore.
  - **Write.** The mode is written to `request.continue_mode` before
    `_set_continue_pending`, so history and `_write_help_key` see it.
  - **Person key.** `_write_help_key` keeps the person key only for
    current_screen (unchanged). So restore or from_scratch on a confirm the
    fault interrupted cancels the open person step row, through the existing
    check after `_set_continue_pending`.
- **Night auto-fix is unchanged.** `report_case_fix` gets no mode, and
  `finish_case_auto_fix` continues with the stored mode. No person is there to
  weigh a riskier choice, and the stored mode is the conservative default.
- **Auto-fix policy residual.** A person's from_scratch Continue is not a
  `TestRunCaseResume` re-run, so Q10 auto-fix eligibility
  (`case_help_policy.auto_fix_eligible`) may match an old step number. This
  residual is already accepted for redesigns (help-chat note) and is noted
  again.
- **Agent recommendation.** `propose_resolution` gains an optional
  `continue_mode` parameter (enum `current_screen | restore | from_scratch`,
  meaningful only with `action=continue`). The handler is
  `help_chat_tools._handle_propose_resolution`:
  - It rejects a mode with another action.
  - It passes the mode to `record_agent_proposal(continue_mode=)`. Under the
    request lock, that service checks the mode against `allowed_continue_modes`
    and raises `RunHelpError`, which becomes the tool error "That mode is not
    allowed for this case: <allowed list>".
  - It stores `agent_proposal_continue_mode`.

  The value is an enum and is never free text, so nothing new reaches a prompt
  or a page unescaped. The tool description says the person chooses, and
  explains when restore or from scratch fits. `get_help_case` and the brief list
  the allowed modes. Night auto-fix turns do not get `propose_resolution`, as
  today.
- `HelpPanel` gains `continue_mode_choices` (the allowed tuple),
  `agent_continue_mode` (the recommendation or `""`) and the pause fields.
  `CaseHelpResolutionForm.continue_mode` is an optional `ChoiceField` (radio).
  Its initial value is the stored mode, never the agent's, and the agent's mode
  is shown as "The agent suggests: …". The view validates the field and the
  service enforces the allowed set.

### Services
- **`projects/case_pause_policy.py`** (new pure leaf):
  - `pause_reason(label, step_num)`, `pause_ask()`;
  - `pause_continue_mode(checkpoint, *, restore_pending)`: None gives
    FROM_SCRATCH, a pending restore gives RESTORE, anything else gives
    CURRENT_SCREEN;
  - `allowed_continue_modes(...)`;
  - the blocker texts.
- **Actor**: reuse `projects.person_step_actor.PersonStepActor` (user or
  None, plus channel). Do not add a new actor type.
- **`projects/case_pause_services.py`** (new; imports `services`,
  `run_reopen_services`, `case_help_services`):
  - `request_case_pause(*, pivot: TestRunTestCase, actor: PersonStepActor,
    message: str = "") -> CasePauseOutcome`. The outcome is a StrEnum:
    `REQUESTED`, `ALREADY_REQUESTED` or `PAUSED`. It raises `CasePauseError`
    (new, `projects/exceptions.py`, user-safe `message`). It locks project,
    then run, then the open person step, then the case, and caps the message
    at `PAUSE_MESSAGE_MAX_LENGTH`.
    - **Running case** (IN_PROGRESS, run STARTED): it writes the
      `pause_requested_*` fields and `pause_message` for the locked generation.
      If a request already exists for this generation, the outcome is
      `ALREADY_REQUESTED`: the first requester and message are kept, and a
      second message is refused with a flash note. On commit it broadcasts the
      case status and the pivot event (with `pause_pending`) and nudges the
      runner.
    - **Waiting person step** (AWAITING_PERSON, run PAUSED, step `waiting`):
      it converts at once and returns `PAUSED`.
      - It cancels the row (`CANCELLED`, close reason `PAUSED`, `closed_by` /
        `closed_via` from the actor).
      - It drops the checkpoint `person` key.
      - It opens the request through the shared row writer below, with
        `continue_mode` CURRENT_SCREEN, or RESTORE for a `kind="restore"`
        hand-off.
      - The case becomes NEED_HELP, and the run stays PAUSED.
      - On commit: broadcast and an attention change, then the auto-open
        (below).
    - **Refused** when:
      - the case is not running and not waiting for a person;
      - it already needs help: "already waits for help, open the help chat"
        (this covers Night auto-fix);
      - the person step is `reported` (the case is IN_PROGRESS, so the running
        branch applies instead);
      - the run is not active;
      - the actor is a web user without write access (the decorator already
        returns 403).
  - `get_case_pause_panel(test_run, pivot) -> CasePausePanel` runs no queries;
    the caller loads `pause_requested_by` and the open step state. Fields:
    - `can_pause`, `pause_blocker`;
    - `pause_pending` (IN_PROGRESS and `pause_requested_generation` set),
      `pause_requested_by_label`, `pause_requested_at_iso`;
    - `paused` (an open request with `is_person_pause`), `paused_by_label`;
    - `discussion_thread_id` (from `HelpPanel.thread` when the caller passes it);
    - `pause_message_state`, `pause_message_note`;
    - `dropped_message` (the text of a pause the verdict beat; see below).
  - `pause_pending_for(pivot) -> bool`: a pure field test for socket payloads
    and lists.
  - `dismiss_dropped_pause(*, pivot, user)`: clears a dropped request's fields
    (Dismiss on the notice).
- **Shared row writer** (`projects/case_pause_rows.py`, a new leaf that imports
  only models, policy and `agents.checkpoint`):
  - `open_pause_help_request(pivot, *, step_num, continue_mode, requested:
    PauseRequestSnapshot, stopped_by_pause: bool, base: HelpRequestInfo | None)`
    builds the `CaseHelpRequest` fields:
    - the paused_by fields;
    - `pause_message` and `pause_message_state = pending` if there is a
      message, or if `paused_by` is set (the thread still opens);
    - with `stopped_by_pause`, the pause reason and ask, and otherwise the
      `base` agent or fault texts.
  - It clears the case's `PAUSE_REQUEST_FIELDS`.
  - It is used by `_pause_run_for_help` (the worker) and by
    `request_case_pause` (the person step conversion), so both write the same
    rows.
- **Worker hooks (`projects/services.py`)**:
  - `_build_cancellation_check`: add `Exists(pivot,
    pause_requested_generation=generation)` to the OR. It is still one query per
    poll.
  - `_mark_pivot_in_progress`: under the case lock, clear a request from
    another generation. A same-generation request is kept, so redelivery
    still pauses.
  - Pre-case `_safe_cleanup` is skipped when the case carries a request for
    this generation (a redelivered task after a dead worker).
  - The `except (AgentCancelledError, ControllerNotConnectedError)` branch:
    when not stale, re-read status and `pause_requested_generation`. An
    IN_PROGRESS case with a request for this generation calls
    `_pause_case_for_person(pivot, generation, restore_baseline)`, which has the
    same shape as `_pause_case_after_fault`. Otherwise cancel as today.
  - `_pause_run_for_help(..., pause: PauseRequestSnapshot | None, stopped_by_pause:
    bool = False)`. Under the run lock, after the generation, abort and status
    checks, it reads the case's pause fields. If a request for this generation
    is present (a pure pause, or a pending pause the agent's `need_help` or a
    fault beat), it does the following:
    - starts no auto-fix and sends no ntfy, because a person is present (Q6:
      the inbox still lists the request);
    - writes the rows through `open_pause_help_request`;
    - logs `[Paused by <label> at step N]` for a pure pause;
    - on commit, calls `enqueue_open_paused_help_chat(request_id)` (see below).

    The fault and agent paths keep their reason, ask, fault and continue mode.
  - `_pause_run_for_person` (a HITL hand-off racing a pause): if a request
    exists for this generation, open the pause request (stopped_by_pause, mode
    CURRENT_SCREEN) instead of the person step row. Keep the `person` key
    `waiting`, so a current-screen Continue re-uses the prepared hand-off.
  - `_finalize_pivot` (the verdict wins, Q3): the request is **dropped**. If it
    carried a message, keep the fields as the "dropped" record. Otherwise clear
    them in the same save. Clear a dropped record at the next pick-up of another
    generation, at Reset, at Re-run, or through Dismiss.
  - `_cancel_pivot_if_in_progress` and `_mark_pivot_cancelled` (abort or
    re-test won): clear the fields in the same UPDATE. The message is lost; the
    user asked to abort or re-test.
  - `reset_test_run`: add `PAUSE_REQUEST_CLEARED` to its bulk update.
- **`projects/case_help_services.py`**: `continue_after_help(continue_mode=)`,
  the `HelpPanel` additions, the Mark failed and blocked texts ("while it was
  paused by X"), and `restore_open_help_after_runner_update` unchanged.
  `clear_agent_proposal` also clears `agent_proposal_continue_mode`.
- **`projects/run_help_mode_services._request_waiting_for_person`**: excludes
  `has_pending_person` requests.
- **`machine_setup`**:
  - `case_help_brief.py` / `services/case_help_context.py`: a
    `stopped_by_pause` request gets "WHY THE CASE STOPPED (a person paused it to
    discuss it; nothing failed)". Every request lists the allowed continue
    modes.
  - `services/help_chat_tools.py`: the `propose_resolution(continue_mode)`
    parameter.
  - New `services/open_paused_help_chat.py::open_paused_help_chat(help_request_id)`.
    It is idempotent and runs in the new task
    `machine_setup.tasks.open_paused_help_chat` (queue `execution`, like
    `start_case_auto_fix`; name in `machine_setup/task_names.py` as
    `OPEN_PAUSED_HELP_CHAT`; `acks_late`, `reject_on_worker_lost`, soft and hard
    time limits 60/90). `projects.case_help_state.enqueue_open_paused_help_chat`
    sends it by name (the same cycle rule as auto-fix). If the broker fails, the
    state is set to `refused` with "Open the discussion yourself", and nothing
    raises. The service does the following:
    1. It loads the request. It does nothing unless the state is `pending` and
       the request is still open.
    2. It resolves the user: `paused_by`, while they are an active writable
       member (`project_access_services`). For an anonymous runner pause, or a
       user who is no longer writable, the state becomes `refused` with "Open
       the discussion on the web to send your message". The message stays on
       the request and nothing is sent.
    3. It calls `open_help_chat(project, pivot, user, text=pause_message)`. An
       empty message sends `DEFAULT_FIRST_MESSAGE` on a new thread, as a manual
       open does.
    4. On success the state becomes `sent`. On `HelpChatSendRefusedError` it
       becomes `refused` with the exception's user-safe message; the thread is
       kept and the message is shown in the thread composer for a re-send. On
       `RunHelpError` or `MachineSetupError` (resolved meanwhile, or archived)
       it becomes `dropped`.
    5. On commit it broadcasts `machine_setup.help` and the case status, so
       open pages navigate.

### Pages and forms

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `projects:test_run_case_pause` `/projects/<id>/test-runs/<run_id>/cases/<pivot_id>/pause/` (new, `projects/case_pause_views.py`) | POST | `@project_membership_required` (read-only gets 403) + `require_POST` | `CasePauseForm`: `message` (optional, max 8000, Textarea), `next` (safe-redirect checked as in the retest view) | Redirect to `next` or the case page, with a flash: "Pausing: the agent stops at its next action; the discussion opens when it has stopped", or "Already pausing (requested by X)", or "Paused; opening the discussion" | `CasePauseError` flash; foreign case 404 |
| `projects:test_run_case_pause_dismiss` `…/cases/<pivot_id>/pause/dismiss/` (new) | POST | as above | none | Redirect to the case page | `CasePauseError` |
| `projects:test_run_case_help_continue` (existing) | POST | unchanged | `CaseHelpResolutionForm.continue_mode` (new, optional) | `_case_help_card.html` and the machine_setup thread help panel render the radios from `help_panel.continue_mode_choices` and the agent's suggestion | `RunHelpError` flash |
| `machine_setup:help_chat_open` (existing) | POST | unchanged | unchanged | fallback "Open discussion" when the state is `refused` or `dropped` | unchanged |
| `machine_setup:result_chat_open` (existing) | POST | unchanged | its `text` is pre-filled with the dropped message | the "Too late to pause" notice | unchanged |

- **Case page** (`projects/views.py` case detail): add `pause_panel` and
  `pause_form` to the context, and add `pause_requested_by` to the view's
  `select_related`. It shows the following:
  - **Before the stop.** A Pause button and dialog
    (`templates/projects/includes/_case_pause_dialog.html`, the `dialog`
    component, with an optional message textarea). While pending it shows
    "Pausing… requested by X".
  - **Paused.** "Paused by X at step N" in `_case_help_card.html`, with the
    message state: "Your message was sent", or "Not sent: <note>" plus a button
    that posts `help_chat_open` with the text.
  - **Verdict won.** A "Too late to pause: the case finished first" notice with
    the dropped message. It has "Discuss the result" (a POST to
    `result_chat_open` with the message pre-filled in an editable textarea; the
    person presses it) and Dismiss. The message is not sent automatically: the
    person asked to steer a running test, not to review a result, and an
    unattended agent turn would spend tokens on a question they may no longer
    have.
- **Live navigation**: the case page's Alpine socket component receives
  `test_run_case.status` with `discussion_url` set (see below) while it showed
  `pause_pending`, and sets `window.location` to that URL. This is a GET of the
  thread page; nothing is posted from JS. The server already created the
  thread and sent the message. If the person closed the page, nothing is lost:
  the thread, the inbox item and the case card all link to it.
- **Run page** `_run_help_banner.html`: "Paused by X" wording, the Pause button
  for the running case (from the loaded pivot list, no extra query), and the
  mode radios in the help panel.
- **Lists** keep "Need help" (Q9).
- No `|safe`. Messages, labels and notes are rendered escaped.

### Background work and WebSockets
- New Celery task `machine_setup.tasks.open_paused_help_chat(help_request_id)`
  on `execution`. The task args hold only the request id, never message text.
- `CaseStatusEvent` gains `pause_pending: bool`, `pause_requested_by: str` and
  `discussion_url: str` (the thread page URL once a `case_help` thread exists
  and `pause_message_state` is `sent` or `refused`, otherwise `""`). Only the
  `open_paused_help_chat` commit broadcast and the worker's status broadcasts
  fill it. Pure status pushes from other paths send `""`, which keeps them free
  of extra queries. `PivotStatusEvent` gains `pause_pending`.
- No controller protocol change in this plan. The runner messages are named in
  the seam below and implemented by the Test Runner window plan.

### Contract for the Test Runner window (`docs/plan/feat-tester-console.md`)
- **Web contract (kept):**
  - the POST URL `projects:test_run_case_pause` (kwargs `project_id`,
    `test_run_id`, `pivot_id`; fields `message`, `next`);
  - the service `projects.case_pause_services.request_case_pause(*, pivot,
    actor: PersonStepActor, message="") -> CasePauseOutcome`, which raises
    `CasePauseError`;
  - the panel `get_case_pause_panel(test_run, pivot) -> CasePausePanel` (fields
    listed above);
  - the socket fields `pause_pending`, `pause_requested_by` and
    `discussion_url`.
- **Runner seam (new, server side only in this plan):**
  `projects/runner_case_pause_services.py::pause_from_runner(*, project_id: int,
  channel_name: str, person_user_id: int | None, pivot_id: int, message: str) ->
  CasePauseOutcome`. It applies the same rules as `answer_from_runner`:
  - the owner connection only (`agent_channel_name`);
  - the shared per-project runner answer throttle (20/min). This is the only
    throttled path, because it is a socket surface;
  - the pivot must be an IN_PROGRESS or AWAITING_PERSON case of a run of that
    project;
  - the actor is `PersonStepActor.runner(<active writable member or None>)`.

  It then calls `request_case_pause`. An anonymous runner pause is allowed, but
  its message is not auto-sent (the state is `refused`, "Open the discussion on
  the web"), because a help-chat turn needs a member user. The console plan may
  define an anonymous chat path.
- **Proposed protocol names** (the console plan owns them in
  `controller_client/protocol.py`):
  - runner to server `case_pause_action` `{request_id, pivot_id, message}`;
  - server to runner `case_pause_action_result` `{request_id, ok, outcome,
    message}`;
  - optional capability `CASE_PAUSE_V1`.

  The consumer (`projects/controller_consumer.py`) dispatches it the way
  `person_step_action` is dispatched. The runner learns which case is pausable
  from the console plan's state snapshot, which should carry `can_pause` and
  `pause_pending` from `get_case_pause_panel`.

### Settings / infrastructure
None.

### Data flow
1. **Request.** Pause (web form, or runner `case_pause_action`) calls
   `request_case_pause`. Under the locks it writes the request (generation,
   actor, message) and broadcasts `pause_pending`.
2. **Worker stops.** The worker's next cancellation poll raises
   `AgentCancelledError`. The last checkpoint has the cursor on the current
   step.
3. **Pause lands.** `_pause_case_for_person` calls `_pause_run_for_help`. Under
   the run lock, `open_pause_help_request` does the following:
   - opens the request (`stopped_by_pause`, paused_by, message, state
     `pending`);
   - clears the case's request fields;
   - sets the case NEED_HELP and the run PAUSED;
   - starts no auto-fix and sends no ntfy.

   The `finally` block runs no teardown and no cleanup, and the chain stands
   down.
4. **Discussion opens.** On commit, `open_paused_help_chat` is enqueued. The
   task opens the `case_help` thread (the brief says paused) and sends the
   message as the pauser's turn. The setup turn runs as usual. The state becomes
   `sent`, and the case status event carries `discussion_url`, so an open case
   page navigates there.
5. **Discussion.** The person and the agent talk: `run_setup_task` on the
   machine, `propose_plan_change`, and `propose_resolution(continue,
   continue_mode=restore)`.
6. **Resolution.** On Continue the person picks an allowed mode (the agent's
   suggestion is shown, the stored mode is pre-selected), or uses the redesign,
   or marks the case failed, blocked or for re-test. The existing services
   resolve the request, credit, record the session activity and wake the run.
   The help key carries `paused_by`, so the state line and the verdict mention
   the pause.

Variants:
- **Waiting HITL step.** Steps 2 and 3 happen at once inside `request_case_pause`.
- **The agent's need_help or a fault won.** The request keeps its own texts and
  mode, records the pending pauser and message, skips auto-fix and ntfy, and
  still auto-opens the discussion.
- **The verdict won.** The request is dropped. A message is kept for the "Too
  late to pause" notice.
- **Abort or re-test won.** The request is discarded.

## Alternatives considered
- **A new case or run status `PAUSED`.** It would duplicate all of Need help.
  Rejected; the help request carries the difference.
- **A `BlockedReason.PAUSED` fault.** It would misuse a "could not be executed"
  enum that drives auto-fix and fault texts. Rejected.
- **Sending the first message from page JS after the status event.** It fails
  when the page is closed and races other tabs. Rejected for the server task.
- **Sending the message from inside `_pause_run_for_help`.** Not possible:
  `projects.services` must not import `machine_setup.services` (cycle), and a
  setup turn must not start inside the worker's transaction. So it is a by-name
  task on commit.
- **Auto-sending a dropped message to the result chat.** Not chosen: it would
  be an unattended agent turn about a question the person may no longer have.
  The message is offered pre-filled instead.
- **Allowing current_screen after runner faults with a warning.** The case
  could pass or fail on a screen that no longer matches its earlier steps.
  Restore or from scratch are cheap and always available. Rejected.
- **A separate `CasePauseRequest` model.** It adds nothing over nullable columns.
  There is at most one request per case, and the generation key makes stale
  values harmless.

## Risks and mitigations
- **The interrupted step is partly done**, and its partial transcript is lost
  (as for faults). The pause state line warns the agent, and the person can
  explain in the chat or the note.
- **Races** (verdict, need_help, fault, re-test, abort): they are decided under
  the existing locks. The verdict and re-test win on the case lock and abort
  wins on the run lock. The pre-verdict check narrows the verdict window. The
  panel always shows the real outcome.
- **The worker dies while pausing.** Redelivery keeps the generation and the
  request, skips the pre-case cleanup, and pauses at the first check. A hard
  kill without redelivery leaves the run STARTED, as today; Abort works.
- **Auto-open failures** (broker, machine not ready, a live turn, the member
  lost access): the state becomes `refused` with a user-safe note. The message
  stays on the request with a one-click re-send, and the inbox lists the
  request.
- **Secrets and injection.** The message is the person's own text, sent as
  their chat turn, the same trust as typing it in the thread. It is never in
  task args or logs. The agent's mode recommendation is enum-validated, and
  labels come from the server.
- **Continue mode on every request**: a broader surface. `allowed_continue_modes`
  is enforced in the service under the locks, the tool validates against it,
  and unsafe current_screen stays disallowed. The new rule that refuses a
  redesign on a restore request is a behaviour change, and it is documented.
- **Night auto-fix.** It is never started for a request with a pending pauser,
  at pause time or after a mode switch. Its own continue mode is unchanged.
- **Migration**: nullable ADD COLUMNs and blank-default CharField/TextField/Boolean
  defaults on populated `projects_testruntestcase` and `projects_casehelprequest`.
  On Postgres 11 and later, a constant default is metadata-only. Deploy: run
  `migrate`, then restart web, Daphne and every Celery worker together (an old
  worker ignores pause requests and an old machine_setup worker lacks the task).
- **N+1.** The panel uses loaded fields, the socket `discussion_url` is computed
  only on the two paths that need it, and the cancellation poll is still one
  query.

## Implementation steps
1. **Shared base.** Write this first; every later step builds on it.
   - Models, `PauseMessageState`, `PersonStepClose.PAUSED`,
     `PAUSE_MESSAGE_MAX_LENGTH`, `PAUSE_REQUEST_*`, migration `0050_case_pause`.
   - `CasePauseError`.
   - `projects/case_pause_policy.py` (`allowed_continue_modes`,
     `pause_continue_mode`, texts) and `projects/case_pause_rows.py`.
   - `CheckpointHelp.paused_by` and its checkpoint read/write.
   - The name `OPEN_PAUSED_HELP_CHAT` in `machine_setup/task_names.py`, and
     `enqueue_open_paused_help_chat` in `projects/case_help_state.py`.
2. **Agent wording and pre-verdict check.** Files:
   `agents/services/orchestrator.py`, `agents/services/orchestrator_prompts.py`.
3. **Worker hooks.** Files: `projects/services.py`.
   - cancellation check, pick-up clear, cleanup skip;
   - except branch and `_pause_case_for_person`;
   - `_pause_run_for_help` and `_pause_run_for_person`;
   - the `_finalize_pivot`, cancel and reset clears;
   - the socket fields.
4. **Pause request service, panel, Dismiss and runner seam.** Files:
   `projects/case_pause_services.py`, `projects/runner_case_pause_services.py`.
5. **Continue mode for every request.** Files:
   `projects/case_help_services.py`, `projects/case_help_proposal_services.py`,
   `projects/forms.py`, `projects/views.py` (pass `continue_mode`),
   `projects/run_help_mode_services.py`.
6. **machine_setup.** Files:
   - `services/open_paused_help_chat.py` and `tasks.py`;
   - `services/help_chat_tools.py` (`continue_mode` parameter);
   - `case_help_brief.py` and `services/case_help_context.py` (pause and
     allowed modes);
   - the thread page help panel template (mode radios, agent suggestion,
     unsent message in the composer).
7. **Pages.** Files:
   - `projects/case_pause_views.py`, `projects/urls.py`, `projects/forms.py`
     (`CasePauseForm`, the same file as step 5, so do it in the same pass or
     after it);
   - `templates/projects/includes/_case_pause_dialog.html`,
     `_case_help_card.html`, `_run_help_banner.html`, and the case detail
     template;
   - `static/js/punk-hazard.js` (`pause_pending` / `discussion_url` handling);
   - `static/css/punk-hazard.css`, if needed.
8. **Docs.** Files: `CLAUDE.md` (Need help: pause, continue mode choice),
   `agents/CLAUDE.md` (`paused_by` help key).

Steps 2, 4 and 6 can run in parallel after step 1. Steps 5 and 7 share
`projects/forms.py`: do them sequentially, or give `forms.py` to one owner.

## Verification
- `pre-commit run --all-files` (strict mypy for every config),
  `python manage.py check`, `python manage.py makemigrations --check`.
- Manual, with a live runner and vision worker, on an Attended multi-step web case:
  1. Pause with a message during step 2. The log stops within one agent action
     with `[Paused by …]`. The case is Need help and the run is Needs help, the
     browser stays open, and no ntfy is sent. The case page navigates to the
     thread, where the message is the first person turn and the agent answers.
  2. Repeat, but close the page before the stop. The thread still exists with
     the message sent, and the inbox lists the request.
  3. In the discussion: `run_setup_task`, `propose_plan_change`, and
     `propose_resolution(continue, continue_mode=restore)`. The suggestion is
     shown and is not pre-selected. An invalid mode is a tool error.
  4. Continue with current screen, then restore, then from scratch (on
     separate pauses), checking each behaviour. A redesign plus restore is
     refused.
  5. Runner fault: stop the Test Runner mid-case. The help request offers
     restore and from scratch, and no current screen. A redesign is refused
     until restore. An agent-asked help request offers all three.
  6. Night: the auto-fix still continues with the stored mode. Pause, then
     switch to Night: no auto-fix starts.
  7. Race: pause during the last step's evaluation. Either it pauses before the
     verdict, or the "Too late to pause" notice shows the message with "Discuss
     the result" (pre-filled) and Dismiss.
  8. Two browsers: the second Pause says already pausing. A read-only member
     gets 403 and sees no button.
  9. HITL: Pause on "Your turn". The case pauses at once, the step row becomes
     Cancelled/Paused, the runner window drops it, and Continue hands out a
     fresh step. Pause during a confirm acts like a running case.
  10. The machine is not READY at auto-open: the state is `refused` with a
      note, and the re-send button works once it is ready.
  11. Kill the worker after Pause: redelivery pauses without a pre-case
      cleanup. Abort while pausing: the run is Cancelled, no request is
      opened, and the message is discarded.
  12. Runner seam (a shell script calling `pause_from_runner`): a non-owner
      channel is refused, the throttle works, and an anonymous pause pauses but
      reports the message as not sent.
- Run `reviewer` and `verifier` (the verifier gets this plan and the original
  request plus the answers below).

## Open questions
All resolved on 2026-10-31.
- Q1. Where does a pause apply? **Answer:** case-level only (running case and
  waiting person step). There is no run-level pause.
- Q2. How soon does the case stop? **Answer:** "Pause now" only (the agent's
  next action). There is no "after this step".
- Q3. What happens if the case finishes first? **Answer:** the request is
  dropped. A first message is offered for the result chat (pre-filled, not
  auto-sent) or can be dismissed.
- Q4. Does Pause go straight into the discussion? **Answer (changed):** yes.
  The dialog takes an optional first message, stored on the case row until the
  pause lands. The server opens the `case_help` thread and sends the message as
  the pauser's turn (task `open_paused_help_chat`). An open page navigates live
  through `discussion_url`.
- Q5. Should the continue mode choice be generalised? **Answer (changed):**
  every help request offers the modes in `allowed_continue_modes`.
  current_screen is withheld when the stored mode is restore (runner faults, an
  unfinished restore, a runner update). The agent may recommend a mode through
  `propose_resolution(continue_mode=)`; it is shown and not pre-selected. The
  Night auto-fix keeps the stored mode.
- Q6. Who hears about a pause? **Answer:** inbox and attention only, no ntfy.
- Q7. Should the pause be recorded or throttled? **Answer:** no credit, no
  session activity and no throttle for the web pause. The runner seam reuses
  the existing runner answer throttle, because it is a socket surface.
- Q8. Can a pending pause be cancelled? **Answer:** no.
- Q9. What should lists call a paused case? **Answer:** lists keep "Need help".
  The case page, run banner, thread and runner window say "Paused by X".

## Changelog
- 2026-10-31: created (Draft).
- 2026-10-31: revised with the user's answers, Status Ready.
  - Q4: a first message stored on the case, then a server-side auto-open and
    send (new task `open_paused_help_chat`, `pause_message_state`), live
    navigation through `discussion_url`, and the dropped message offered for
    the result chat.
  - Q5: the continue mode choice on every request (`allowed_continue_modes`,
    current_screen withheld after restore-forcing faults, a redesign refused on
    a restore request), and the `propose_resolution(continue_mode)`
    recommendation. The Night auto-fix is unchanged.
  - The web tester console was dropped: added the Test Runner seam
    (`runner_case_pause_services.pause_from_runner` and the proposed
    `case_pause_action` / `case_pause_action_result` / `CASE_PAUSE_V1`), and
    `request_case_pause` now takes a `PersonStepActor`.
  - `CaseHelpRequest` gains `stopped_by_pause`, so a pending pauser can be
    recorded on a request that the agent's need_help or a fault stopped first.
- 2026-10-31: the runner protocol for pause (`case_pause_action` / `case_pause_action_result`, capability `CASE_PAUSE_V1`) is owned by `docs/plan/feat-runner-person-window-v2.md`; that plan calls `pause_from_runner` unchanged and keeps the shared 20/min runner throttle.
- 2026-10-31: implemented. Review pass 1 (reviewer + verifier) findings:
  - Fixed: live navigation race (the case page no longer reloads on the
    paused `need_help` event; it waits for `discussion_url`, falls back to a
    reload with `?discussion=1`, and a page loaded with that flag or a pending
    pause follows an already opened discussion); pauser labels capped at 255;
    the mode choice also shows when the stored mode is not allowed
    (`HelpPanel.choose_continue_mode`); the case page reloads when a pending
    pause loses to the verdict; a manual send also clears a `dropped` message;
    the pause dialog is gated on write access; the run page hides Pause once
    `pause_pending`; the help brief broadcast runs on commit; the paused
    discussion task no longer defers the checkpoint it then reads.
  - Partly applied (reviewer, shared "pause -> Need help" tail): the two
    pause-only writers use `case_pause_rows.mark_case_paused_for_help` /
    `PAUSED_CASE_FIELDS`; `_pause_run_for_help` keeps its own write because it
    serves the agent and fault paths too.
  - Not applied as suggested (reviewer, view reads the requester label):
    `request_case_pause` keeps returning `CasePauseOutcome` (the Test Runner
    window plan consumes it); the view asks `get_pause_requester_label`.
  - `dismiss_dropped_pause` lost its unused `user` parameter.
  - Accepted residual: a Needs re-test mark on a finished case does not clear
    a dropped record; Re-run, Reset, Dismiss or the next pick-up do.
- 2026-10-31: review pass 2 fixes: Continue pre-selects the first allowed mode
  when the stored one is not allowed (`HelpPanel.default_continue_mode`); a
  case page that loads while the discussion is opening arms the same fallback
  reload; the `?discussion=1` flag is dropped from the URL once read.
