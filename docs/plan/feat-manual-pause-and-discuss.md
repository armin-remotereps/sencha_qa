# feat: Pause and Discuss for Manual runs

Status: Done
Created: 2026-11-01
Request: In a Manual run, Pause and discuss is hidden today (refused with
`MANUAL_PAUSE_REFUSED`). That is wrong. A tester doing a manual case must be
able to pause it, for example to take a break, and come back to the same case
with the steps already marked still in place. The tester must also be able to
discuss the case with the AI. The user calls Discuss "critical for the
tester". Both must work from the Test Runner window, where manual testing
happens, and from the web case page. Because a manual run has no agent worker,
no chain and no help requests, the agent-mode design (cancellation check →
Need help request → `case_help` thread) does not fit. Manual runs need their
own, simpler meaning for both features.

## Context

- **Agent-mode Pause and discuss** (`docs/changed/2026-10-31-pause-and-discuss.md`):
  `projects/case_pause_services.request_case_pause` stores a request on the
  running case (`TestRunTestCase.pause_requested_*`). The worker's
  `_build_cancellation_check` stops the case and opens a Need help
  `CaseHelpRequest` (`case_pause_rows.open_pause_help_request`).
  `machine_setup.tasks.open_paused_help_chat` then opens the `case_help`
  thread. In a Manual run, `request_case_pause` and `_can_pause` refuse with
  `MANUAL_PAUSE_REFUSED` (`projects/manual_run_policy.py`), and the case
  page's "Pause and discuss" button is hidden because `pause_panel.can_pause`
  is false.
- **Runner window, agent side** (`docs/changed/2026-10-31-runner-person-window-v2.md`):
  `projects/runner_case_services.get_runner_cases` gives the IN_PROGRESS case
  of a STARTED run the `RUNNING` role, with a pause panel, for CASE_PAUSE_V1
  runners. It does not filter by mode, so today a manual claimed case shows up
  there as a "running" case with a Pause button blocked by
  `MANUAL_PAUSE_REFUSED`. The Discussion tab (`person_window_app.py`
  `_build_discussion_tab` / `_render_discussion`) handles help cases only. It
  is fed by `machine_setup/services/runner_discussion.py`
  (`get_runner_discussion`, `get_runner_send_blocker`, `send_from_runner` via
  `open_help_chat`), and `projects/runner_window_state.py` composes it into
  the snapshot. `machine_setup/events.py` nudges the runner on every
  top-level message, turn and help broadcast.
- **Manual run** (`docs/changed/2026-11-01-manual-run-mode.md`, `-resume.md`,
  `-step-howto.md`):
  - Claim and release are in `projects/manual_case_claim_services.py`. Claim
    and hand-in both lock project → run → case through
    `manual_case_lock.lock_manual_run_case`. At most one claimed case per run.
    A claim sets IN_PROGRESS, opens a duration segment
    (`case_duration_services.open_case_segment`), mints `manual_claim_token`
    and writes the verbatim-step checkpoint.
  - Every path that ends a claim clears the claim through
    `MANUAL_CLAIM_FIELDS` / `MANUAL_CLAIM_CLEARED` (`projects/models.py`):
    release, hand-in, abort (bulk `update(**MANUAL_CLAIM_CLEARED)` in
    `services.abort_test_run`), Needs re-test, Re-run and Reset
    (`services.py` ~2777).
  - The tester's step marks (Pass/Fail, notes, shots) exist **only in the
    runner's spool and window** until the hand-in
    (`manual_coordinator.py`, `manual_spool.py`). The server learns them from
    `manual_case_submit`.
  - Snapshot: `projects/runner_manual_state_services.build_manual_run_state`
    → `manual_run_state` (`ManualActiveCasePayload`, `ManualCaseListItemPayload`
    in `controller_client/protocol.py`). It is pushed on the same nudge as
    `person_step_state`. CLIENT_VERSION is 0.15.0.
  - Runner messages `manual_case_action` (`ManualCaseActionKind`: claim,
    release; answered by `manual_case_action_result`), `manual_case_submit`
    and `manual_screenshot_upload` are routed by
    `projects/runner_manual_services.py`. They are accepted from the owner
    connection only, with the shared 20/min runner throttle.
  - Ask AI per manual step: `person_step_question_services.ask_manual_step_question`,
    the advisor on `assist`, with Q&A history per step. It is advisory only.
  - Durations: `active_duration` (sum of closed segments) feeds the case page
    and TestRail elapsed (`testrail_results_services`,
    `testrail_checked_push_services`).
  - There is no claim timeout or claim sweep. The only manual sweep is
    `sweep_stalled_manual_uploads`.
- **Setup threads** (`machine_setup`): `SetupConversationKind` is chat,
  auto_fix, case_help or case_result. There is one non-archived
  `case_result` thread per case (`setup_one_result_thread_per_case`).
  `thread_role.thread_role()` picks the role from the thread's FKs.
  `setup_tools.main_tool_definitions(role)` gives every role the
  project-data tools, `run_setup_task` and the KB tools, plus role tools.
  `setup_prompt` has a rules block per role.
  - Send gates (`services/send_gates.py`): one live turn per project; any
    STARTED run refuses a turn (`run_and_update_refusal`); help, result and
    auto-fix threads also need the machine READY (runner + visual engine).
    A manual run is STARTED, so **today every setup thread is refused while a
    manual run runs**.
  - `run_setup_task(read_only=)` is enforced by the prompt only.
  - Turns run on the `execution` queue (`machine_setup.tasks.run_setup_turn`).
- Import rules: `projects.services` never imports `machine_setup.services`
  (use leaf modules or send tasks by name). `projects/runner_window_state.py`
  and `controller_consumer.py` already import
  `machine_setup.services.runner_discussion`. `case_result_page.py` reads
  `machine_setup.models` from `projects`.
- Versions: Django ~=5.2.11, channels ~=4.3.2, celery ~=5.6.2. The plan uses
  only plain model fields, `UniqueConstraint(condition=)` and the existing
  socket and task patterns. No new library behaviour.

## Goals

- **Manual Pause**: the tester (or a writable member on the web) pauses the
  claimed case and continues it later.
  - The claim, the marked steps in the runner spool and the claim token stay
    unchanged.
  - The case stays IN_PROGRESS and the run stays STARTED. The case shows
    "Paused by X since HH:MM" on the run page, the case page and the runner
    window.
  - Paused time does not count in `active_duration`, so it does not reach
    TestRail elapsed.
- **Manual Discuss**: a multi-turn chat with the setup agent about the
  claimed manual case, from the runner window and the web case page, whether
  or not the case is paused.
  - The agent sees the case text, the verbatim steps, the how-to guide, the
    Ask AI Q&A, the step marks so far (reported by the runner), the KB, the
    Application context and the artifacts profile.
  - It proposes nothing that changes the case: no verdicts, no redesigns.
- Older Test Runners keep working unchanged. Their window just has no
  Pause/Discuss.

## Non-goals

- No new case or run status. No run-level pause. The run is not PAUSED,
  because that status means "Needs help" everywhere.
- No Need help request, continue modes, redesign, Night auto-fix, ntfy,
  credit or test-session activity for a manual pause or a discussion.
- No automatic expiry or release of a long pause (Q5).
- No AI change to the hand-in: the discussion never marks steps, writes a
  verdict or edits notes. The hand-in stays the tester's.
- No change to Ask AI. It stays the per-step quick answer, next to Discuss.
- No change to the result chat of a handed-in manual case.
- No live per-mark progress on the web page (Q4).

## Design

### Data model

`projects` migration `0055_manual_pause_discuss` (additive, nullable or blank
defaults, no backfill):

- `TestRunTestCase`:
  - `manual_paused_at` (DateTimeField, null): set means paused.
  - `manual_paused_by`: FK user, SET_NULL, `related_name="+"`,
    `db_index=False`.
  - `manual_paused_label`: CharField 255, server-written actor label.
  - `manual_paused_via`: `PersonStepChannel`, blank.
  - All four are added to `MANUAL_CLAIM_FIELDS` / `MANUAL_CLAIM_CLEARED`. That
    way every path that already ends a claim (release, hand-in, abort,
    re-test, Re-run, Reset) also ends the pause, with no new hook. Claim must
    write them cleared explicitly, because it saves `*MANUAL_CLAIM_FIELDS`.
  - These are separate from `pause_requested_*`. Those fields mean "the agent
    worker should stop"; their pending/dropped logic
    (`pause_pending_for`, "Too late to pause") must never fire for manual
    cases.
- New `ManualCaseProgress` (one per run case, `OneToOne` to
  `TestRunTestCase`, CASCADE):
  - Fields: `claim_token`, `current_step` (int, null),
    `steps` (JSON list `{num, outcome: pass|fail, note}`), `reported_at`.
  - It is the runner's latest report of the marks so far. The case's current
    claim token fences it: a reader ignores a row whose token is not the
    case's current `manual_claim_token`, so no clearing path needs touching.
    Overwritten in place (upsert).
  - It is advisory only. The hand-in stays authoritative and never reads it.
  - It is a separate table, not a pivot column, so the run page's pivot list
    never loads it.

`machine_setup` migration `0011_manual_case_thread`:

- `SetupConversationKind.MANUAL_CASE = "manual_case", "Manual case discussion"`.
- `SetupConversation.manual_case`: FK `projects.TestRunTestCase`, CASCADE,
  null, `related_name="manual_conversations"`.
- Constraint `setup_one_manual_thread_per_case`: unique `manual_case` where
  `manual_case` is not null and `archived_at` is null. This mirrors the
  result thread constraint. It is a separate FK so that, after hand-in, the
  case can still get its own `case_result` thread without colliding.

### Services

`projects` (new, one use case each):

- `projects/manual_case_pause_services.py`
  - `pause_manual_case(*, project, pivot_id, actor, claim_token: str | None)`:
    - Lock through `lock_manual_run_case`.
    - Requires a STARTED manual run and an IN_PROGRESS case with a claim. A
      runner call must also match the claim token; `None` is a web writable
      member.
    - Already paused → idempotent outcome.
    - `close_case_segment`, set the `manual_paused_*` fields, broadcast and
      nudge on commit.
  - `continue_manual_case(...)`, same shape:
    - Requires a paused claimed case.
    - Refused while a live turn of this case's manual discussion is running a
      machine sub-task, with the message "The agent is working on the
      machine…". The check is a leaf query through `machine_setup.turn_state`
      (which is already importable).
    - `open_case_segment`, clear the pause fields, broadcast and nudge.
  - `ManualPausePanel` dataclass (no queries) for the case page, run page and
    snapshot: `paused`, `paused_label`, `paused_at_iso`, `can_pause`,
    `can_continue`, `blocker`.
  - Errors: `ManualRunError` (existing, user-safe).
- `projects/manual_case_progress_services.py`:
  - `record_manual_progress(*, project, pivot_id, claim_token, current_step,
    steps)`: claim-token check without locking the run (a row lock on the
    case only), validate step numbers against the checkpoint plan, then
    upsert.
  - `load_manual_progress(pivot) -> ManualProgressView | None`: one query,
    fenced by the claim token.
- `projects/manual_run_policy.py`: new user-safe strings: not claimed, already
  paused, not paused, agent busy on the machine. Reword
  `MANUAL_PAUSE_REFUSED` to point at the manual card or runner, because the
  agent-path URL still refuses manual cases (defence in depth).
- `projects/manual_case_submit_services.py`: no logic change. A hand-in while
  paused is accepted: an offline-queued submit can arrive after a web pause.
  `close_case_segment` is already idempotent, and the pause fields clear with
  the claim. Add only a docstring line.
- `projects/runner_manual_services.py`: route `ManualCaseActionKind.PAUSE` /
  `CONTINUE` and the new progress message, under the same owner check and
  throttle. Progress uses its own throttle key (30/min/project) so it never
  starves claim and hand-in.
- `projects/runner_manual_state_services.py`: for runners with the new
  capability:
  - `active` gains the pause fields.
  - The case list item status reads `paused` for the paused claimed case.
  - No discussion here: `projects` cannot import `machine_setup.services`.
- `projects/runner_case_services.get_runner_cases`: exclude manual runs from
  the `RUNNING` role (`test_run__help_mode != manual`). This fixes the stray
  agent Pause shown for manual cases today.
- `projects/manual_case_display_services.py` (case page `ManualCasePanel`):
  add the pause panel, the progress view, `discussion_thread_id` (reads
  `machine_setup.models.SetupConversation`, as `case_result_page.py` does),
  `can_discuss` and `discuss_blocker`.
- `projects/services.py`: `PivotStatusEvent` / `CaseStatusEvent` gain
  `manual_paused` (bool) and `manual_paused_by` (label), filled from the
  pivot with no extra query.

`machine_setup` (new, plus role wiring):

- `machine_setup/services/manual_case_context.py`:
  - `load_manual_case(pivot_id)`, one query.
  - `build_manual_brief(...)`: the static case brief, fenced as data. It
    holds the title, preconditions, verbatim steps with expected results, the
    runner OS stored at claim, and the rule "a person performs every step".
  - `manual_case_state_text(...)` for the tool: status, claim, pause, progress
    (`load_manual_progress`), the how-to (`manual_step_guide_read.load_manual_guide`),
    the case's Ask AI Q&A and the manual hand-in if one exists. Reuse the
    `projects.advice_context.build_advice_context` pieces where they fit,
    rather than copying the case and step formatting.
- `machine_setup/services/open_manual_chat.py`:
  - `open_manual_chat(*, project, pivot, actor, text)`, mirroring
    `open_result_chat`.
  - Lock project → run, require a claimed IN_PROGRESS case of a STARTED
    manual run, then get or create the thread with the brief as its first
    transcript row. An empty `text` sends nothing; there is no default
    message.
  - Sends through `send_setup_message`. A refusal raises a new
    `ManualChatSendRefusedError(conversation_id=)`, and the thread is kept.
- `machine_setup/services/manual_chat_tools.py`, role `MANUAL_CASE`:
  - `get_manual_case()` returns the state text above.
  - There are no proposal tools: no `propose_plan_change`,
    `propose_result_change` or `propose_resolution`.
- `machine_setup/thread_role.py`: `ThreadRole.MANUAL_CASE` and a
  `manual_case_id` argument. `run_turn.py` passes `conversation.manual_case_id`.
- `machine_setup/services/setup_tools.py`:
  - `MANUAL_CASE` gets the project-data tools, the KB tools,
    `get_manual_case`, and `run_setup_task` (no approval id).
  - At call time the `run_setup_task` handler in this role refuses unless the
    case is paused (Q1: "Pause the case first: the tester is using the
    machine") and the machine is READY (the existing call-time check).
- `machine_setup/services/setup_prompt.py`: a MANUAL CASE CHAT RULES block.
  - A person performs and judges every step. The agent advises, explains and
    looks things up in the KB first.
  - It never claims to have marked a step or decided a result, and never
    tells the person to change the verdict without evidence.
  - Machine work happens only when the person asks and only while the case
    is paused, read-only unless asked otherwise. It never changes the
    application state under test without explicit consent.
  - KB and profile text are fenced as data.
- `machine_setup/services/send_gates.py` / `send_message.py`: a manual-case
  thread
  - skips the STARTED-run refusal for its own manual run (any other STARTED
    run still refuses; a project has one active run, so this is defensive);
  - skips the READY gate, like the main chat, because machine tools check at
    call time;
  - is refused when its case is no longer claimed and IN_PROGRESS, with the
    message "This case was handed in or released. Use Discuss result after
    hand-in".
  - The one-live-turn-per-project rule is unchanged: a manual discussion turn
    and the main chat queue behind each other.
- `machine_setup/services/runner_discussion.py`:
  - `get_runner_manual_discussion(project, pivot_id, *, send_blocker)` returns
    the same `RunnerDiscussionView`, keyed by `manual_case` instead of
    `help_request`.
  - `send_manual_from_runner(*, project_id, channel_name, person_user_id,
    pivot_id, claim_token, text)`: owner check, shared runner chat throttle
    (10/min), claim-token check, then `open_manual_chat`.
- `machine_setup/services/chat_page.py` + thread template: a header for the
  manual thread (case title, claimed by, Paused badge, link back to the case
  page). Add the kind to `CASE_THREAD_KINDS` so it lists with the other case
  threads.
- `projects/runner_window_state.get_runner_manual_state`: compose
  `active.discussion` from `get_runner_manual_discussion` for capable runners.
  This module may import `machine_setup.services`.

### Pages and forms

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `projects:test_run_case_manual_pause` `projects/<id>/test-runs/<run>/cases/<pivot>/manual/pause/` | POST | `@project_membership_required` (read-only → 403) | `ManualPauseForm` (`next`) | redirect to case page (or safe `next`) | `ManualRunError` → flash |
| `projects:test_run_case_manual_continue` `…/manual/continue/` | POST | same | `ManualPauseForm` | redirect | `ManualRunError` → flash |
| `machine_setup:manual_chat_open` `machine-setup/<id>/manual/<pivot_id>/` | POST | same | `ManualChatOpenForm` (`text`, ≤ `SETUP_MESSAGE_MAX_LENGTH`, may be empty) | redirect to the thread page | `MachineSetupError` → flash, back to case; `ManualChatSendRefusedError` → thread page with flash |
| Case page (existing) | GET | membership | — | `manual_result` gains `pause` (`ManualPausePanel`), `progress`, `discussion_thread_id`, `can_discuss`, `discuss_blocker`. The Manual test card shows "Paused by X since …", Pause / Continue, "Discuss" (opens a dialog with an optional first message) or "Go to discussion", and "Progress reported by the Test Runner at …" (step outcomes and notes, escaped). The agent "Pause and discuss" stays hidden for manual cases. | — |
| Run page (existing) | GET | membership | — | The Reason cell shows "Paused · At the Test Runner: X". The row is live through `pivot_status.manual_paused`. | — |
| Thread page (existing `machine_setup:thread`) | GET | membership | — | manual header (above) | — |

All runner-sent text (notes, labels, messages) is rendered escaped. There is
no `|safe`.

### Background work and WebSockets

**Controller protocol 0.16.0**: new capability `MANUAL_PAUSE_DISCUSS_V1`
(`"manual_pause_discuss_v1"`), advertised only together with `MANUAL_RUN_V1`
(dropped with the window). One capability, because the window ships both
halves together and the server gates them as a unit.

- runner → server `manual_case_action`: `ManualCaseActionKind` gains `pause`
  and `continue`, both with a required `claim_token`. They are answered by
  the existing `manual_case_action_result {ok, message}`. They are accepted
  only from runners advertising the capability.
- runner → server `manual_case_progress` (new): `{pivot_id, claim_token,
  current_step | null, steps: [{num, outcome: pass|fail, note}]}`.
  - Caps: steps ≤ `MANUAL_CASE_MAX_STEPS`, note ≤
    `MANUAL_STEP_NOTE_MAX_CHARS`, raw ≤ 256 KiB, checked before parsing.
  - No reply; refusals are logged.
  - The window sends it debounced (≥ 2 s after the last mark change, and
    only when the content changed). It is also flushed just before a
    `pause` and a `manual_chat_send`. Ordering on the one socket guarantees
    the agent sees fresh marks.
- runner → server `manual_chat_send` (new): `{pivot_id, claim_token, text}`,
  with text ≤ the existing person chat cap. Answered with
  `manual_case_action_result`.
- server → runner `manual_run_state`, for capable runners only:
  - `active` gains `paused`, `paused_by`, `paused_at`, `can_pause`,
    `can_continue` and `pause_blocker`.
  - `active` also gains `discussion: {thread_url, turn_live, can_send,
    send_blocker, messages[]}`, reusing `PersonDiscussionMessagePayload` and
    the 40-message / 4000-char caps. That is at most about 640 KB, well under
    `MANUAL_RUN_STATE_SAFE_BYTES` (3 MiB). If the estimate would exceed it,
    the oldest messages are dropped first.
  - The list item `status` may read `paused`. It is a string, so older
    windows are unaffected, but the server sends it only to capable runners.
- Pushes: pause and continue broadcast through `broadcast_run_and_pivots`,
  which already nudges the runner. Discussion rows and turns already nudge
  through `machine_setup/events.py`. There is no new push path.
- Consumer (`projects/controller_consumer.py`): handlers for
  `manual_case_progress` and `manual_chat_send`, plus the capability gate on
  the new action kinds. Each runs: owner connection, raw-length cap, form
  (`projects/forms.py`: `RunnerManualProgressForm`,
  `RunnerManualChatSendForm`), then the service through `sync_to_async`.
- **Runner window** (`controller_client/`):
  - Manual tab (`person_window_manual.py`):
    - Pause button. While paused it shows a "Paused since HH:MM" banner with
      **Continue testing**, and disables Pass / Fail / Retake / case
      screenshot / Submit locally. Release, Ask AI and Discuss stay enabled.
      Captures are refused locally while paused.
    - A **Discuss** section reuses the Discussion rendering. Extract the
      chat widget from `person_window_app.py` into a shared
      `person_window_chat.py`, as `person_window_how_to.py` was extracted for
      the how-to. It shows "Open on Punk Hazard".
  - `manual_coordinator.py`: debounced progress, pause and continue actions,
    and chat send; it flushes progress before pause and send.
  - `person_window_ipc.py`: new window → runner events (`manual_pause`,
    `manual_continue`, `manual_chat_send`) and the runner → window fields.
  - `client.py`: routing. `protocol.py`: types, parsers, capability and
    `CLIENT_VERSION`.
  - The window is still hidden for every agent action (`hidden_for_action`),
    so a paused-case machine sub-task never captures it.
- Celery: no new task. Discussion turns use `machine_setup.tasks.run_setup_turn`
  (`execution`) as every setup turn does. The pause is synchronous: there is
  no worker to wait for.

**Older runners** (MANUAL_RUN_V1 without the new capability) get today's
snapshot. A web pause still works on the server: it stops the clock, and the
web shows it. The old window does not know about it. The tester can keep
marking, and the hand-in is accepted and clears the pause. The Discuss
thread works from the web. Runner-side Pause and Discuss need a runner
update (Environment page).

### Settings / infrastructure

No new env var. The throttles reuse existing constants; the progress throttle
is a module constant like `RUNNER_CHAT_LIMIT`. `example.env` is unchanged.
Deploy order:

1. Run `migrate` for `projects.0055` and `machine_setup.0011`.
2. Rebuild the server image (the shared protocol).
3. Restart Daphne/web and the `execution` workers.
4. Update runners from the Environment page.

### Data flow

1. **Pause at the runner**:
   1. Pause in the window sends `manual_case_progress` (flush), then
      `manual_case_action{pause}`.
   2. The consumer routes it to `runner_manual_services`, then
      `pause_manual_case`: lock, close the segment, set the fields.
   3. On commit it broadcasts (run page and case page) and nudges, which
      sends a fresh `manual_run_state` with `paused`.
   4. The window shows the banner.
2. **Continue**: the reverse. The segment reopens and the clock runs again.
3. **Discuss at the runner**:
   1. The window sends the progress flush, then `manual_chat_send`.
   2. `send_manual_from_runner` → `open_manual_chat` creates or reuses the
      `manual_case` thread (with the brief) and calls `send_setup_message`
      (manual gates).
   3. The turn runs on `execution` with role MANUAL_CASE, calls
      `get_manual_case`, searches the KB and answers.
   4. `machine_setup.events` broadcasts and nudges, and the snapshot carries
      the new rows to the window. The web thread page streams as usual.
4. **Discuss on the web**: Discuss on the Manual test card POSTs to
   `machine_setup:manual_chat_open`, which goes to the same service and
   redirects to the thread page.
5. **Hand-in**: the claim and pause clear with `MANUAL_CLAIM_CLEARED`. The
   progress row is fenced out by the token. The manual thread stays readable
   and its sends are refused. Discuss result (the existing result chat) takes
   over.

## Frozen interfaces (foundation done)

Written by the orchestrator before the parts; parts build on them and do not
change them.

- Done: `projects/models.py` `manual_paused_at/_by/_label/_via`,
  `MANUAL_PAUSE_FIELDS` / `MANUAL_PAUSE_CLEARED` (inside
  `MANUAL_CLAIM_FIELDS` / `MANUAL_CLAIM_CLEARED`), `ManualCaseProgress`;
  `machine_setup/models.py` `SetupConversationKind.MANUAL_CASE`,
  `SetupConversation.manual_case` (SET_NULL like `result_case`), constraint;
  migrations `projects.0055_manual_pause_discuss`,
  `machine_setup.0011_manual_case_thread`.
- Done: `controller_client/protocol.py` (CLIENT_VERSION 0.16.0,
  `ClientCapability.MANUAL_PAUSE_DISCUSS_V1`, `MessageType.MANUAL_CASE_PROGRESS`
  / `MANUAL_CHAT_SEND`, `ManualCaseActionKind.PAUSE` / `CONTINUE` (token
  required), `ManualDiscussionPayload`, `ManualActiveCasePayload` pause fields
  + `discussion`, `ManualProgressStepPayload`, `ManualCaseProgressPayload`,
  `ManualChatSendPayload`, `parse_/serialize_manual_case_progress`,
  `parse_/serialize_manual_chat_send`, `MANUAL_CASE_PROGRESS_MAX_BYTES`,
  `MANUAL_CHAT_SEND_MAX_BYTES`). Older runners get the new `active` keys at
  their defaults and ignore them (as for MANUAL_STEP_GUIDE_V1), instead of a
  byte-identical snapshot.
- Done: `projects/forms.py` `RunnerManualCaseActionForm` (token required for
  every action but claim), `RunnerManualChatSendForm`, `ManualPauseForm`;
  progress is validated with `parse_manual_case_progress_payload` (as the
  hand-in is). `projects/manual_run_policy.py` strings `MANUAL_PAUSE_*`,
  `MANUAL_DISCUSS_*`. `machine_setup/thread_role.py`
  `ThreadRole.MANUAL_CASE`, `thread_role(manual_case_id=)`.
  `machine_setup/exceptions.py` `ManualChatSendRefusedError`.
  `machine_setup/turn_state.py` `manual_case_turn_live(pivot_id)`.
- Decision: Continue is refused while any turn of the case's manual discussion
  is live (`manual_case_turn_live`), not only during a machine sub-task: turns
  record no sub-task state, and the tester can Stop the turn.

Part A (`projects`, pause and progress) exposes:

```python
# projects/manual_case_pause_services.py
@dataclass(frozen=True)
class ManualPausePanel:
    paused: bool
    paused_label: str          # "Ana via Test Runner"; "" when not paused
    paused_at: datetime | None
    can_pause: bool
    can_continue: bool
    blocker: str               # user-safe; "" when an action applies
def get_manual_pause_panel(test_run: TestRun, pivot: TestRunTestCase) -> ManualPausePanel  # no queries
def pause_manual_case(*, project: Project, pivot_id: int, actor: PersonStepActor, claim_token: str | None) -> bool  # False: was already paused
def continue_manual_case(*, project: Project, pivot_id: int, actor: PersonStepActor, claim_token: str | None) -> None
# both raise ManualRunError (user-safe); claim_token None = web writable member

# projects/manual_case_progress_services.py
@dataclass(frozen=True)
class ManualProgressStep:
    num: int
    outcome: str               # "pass" | "fail"
    note: str
@dataclass(frozen=True)
class ManualProgressView:
    current_step: int | None
    steps: tuple[ManualProgressStep, ...]
    reported_at: datetime
def record_manual_progress(*, project: Project, pivot_id: int, claim_token: str, current_step: int | None, steps: Sequence[ManualProgressStepPayload]) -> None  # ManualRunError
def load_manual_progress(pivot: TestRunTestCase) -> ManualProgressView | None  # one query, fenced by claim token

# projects/runner_manual_services.py
act_from_runner(...)  # existing signature; now also PAUSE / CONTINUE
def progress_from_runner(*, project: Project, channel_name: str, payload: ManualCaseProgressPayload) -> None  # ManualRunError
```

Part C (`machine_setup`, discussion) exposes:

```python
# machine_setup/services/open_manual_chat.py
def open_manual_chat(*, project: Project, pivot_id: int, actor: PersonStepActor, text: str) -> SetupConversation
# MachineSetupError when the case is not a claimed IN_PROGRESS case of a
# STARTED manual run (nothing created); ManualChatSendRefusedError when the
# thread is kept but the send was refused. Empty text sends nothing.

# machine_setup/services/runner_discussion.py
def get_runner_manual_discussion(project: Project, pivot_id: int) -> ManualDiscussionPayload  # fixed query count
def send_manual_from_runner(*, project_id: int, channel_name: str, person_user_id: int | None, pivot_id: int, claim_token: str, text: str) -> None  # MachineSetupError

# URL machine_setup:manual_chat_open  (POST, args: project_id, pivot_id; form OpenManualChatForm.text)
```

Part B (`projects` web) URLs: `projects:test_run_case_manual_pause`,
`projects:test_run_case_manual_continue` (POST, kwargs like
`test_run_case_manual_release`, form `ManualPauseForm`).

## Alternatives considered

- **Reuse the agent pause (Need help request + `case_help` thread)**:
  rejected.
  - A manual case would leave IN_PROGRESS for NEED_HELP and the run would go
    PAUSED ("Needs help").
  - It would drag in continue modes, auto-fix, wake blockers and the
    `MANUAL_HELP_REFUSED` invariant, none of which mean anything without an
    agent.
- **Pause only in the window (no server state)**: rejected.
  - The clock keeps running into TestRail elapsed.
  - The web cannot see that the tester is away.
  - A runner restart loses the state.
- **Reuse the `pause_requested_*` columns**: rejected. Their pending, dropped
  and "Too late to pause" semantics would misfire on manual cases. Four small
  columns are clearer.
- **Discuss as a multi-turn extension of Ask AI**: rejected.
  - Ask AI is a per-step advisor with no tool loop beyond the KB. It has no
    thread page, no Stop, and no machine access.
  - A setup thread gives a proper chat page, the existing runner Discussion
    rendering, streaming and Stop for free.
  - Ask AI stays for quick per-step questions.
- **Progress inside the checkpoint**: rejected. The hand-in rewrites the
  checkpoint, many readers parse it, and an unknown key risks being dropped.
  A separate fenced row is isolated.
- **One capability per half**: not needed. They ship together. One flag
  keeps the gating simple.
- **No abstraction added**: there is no generic "case discussion" base over
  the help, result and manual threads. The three roles differ in gates and
  tools. A per-role spec refactor was already rejected in the result chat
  plan as out of scope.

## Risks and mitigations

- **Agent and tester on one machine at once.** `run_setup_task` is allowed
  only while the case is paused (Q1), checked at call time. Continue is
  refused while this thread's turn has a live machine sub-task. The window
  hides for agent actions. `read_only` is prompt-only (accepted, as in the
  other roles).
- **Send-gate exemption widens the STARTED-run rule.** It applies only to a
  `manual_case` thread whose case's run is the STARTED manual run. The main
  chat and the help and result chats are unchanged. This needs a review
  focus.
- **One live setup turn per project**: a long discussion blocks the main
  chat. This is acceptable: a manual run already refuses the main chat.
- **Stale pause after an old-runner hand-in**: cleared with the claim.
- **Clock accounting**: pause and continue use the idempotent
  `close_case_segment` and `open_case_segment`. A double pause is a no-op.
  Abort, hand-in and release on a paused case close nothing twice.
- **XSS**: notes, labels and chat text are escaped. The brief and the tool
  output fence runner text as data in prompts.
- **Prompt injection through tester notes and KB**: fenced as data. The
  agent has no tools that change the case.
- **Lock order**: project → run → case (`lock_manual_run_case`). The thread
  open locks project → run first, like `open_result_chat`.
- **N+1 queries**: the snapshot adds one query for the progress (only when
  claimed) and a fixed three or so for the discussion (thread, rows, live
  turn). The case page adds a fixed two. Pivot events add none.
- **Runner flood**: progress is debounced on the runner and throttled on the
  server under its own key, with size caps before parsing.
- **Protocol parse tightening** (the manual-mode note): the new messages are
  new parsers. Extending `ManualCaseActionKind` must not change how the
  existing claim/release parse.

## Implementation steps

0. **Foundation** (orchestrator, before the parts):
   - Models and both migrations (`projects/models.py`,
     `projects/migrations/0055_*`, `machine_setup/models.py`,
     `machine_setup/migrations/0011_*`).
   - `MANUAL_CLAIM_FIELDS` / `MANUAL_CLAIM_CLEARED`.
   - Protocol types, parsers, capability and `CLIENT_VERSION` 0.16.0
     (`controller_client/protocol.py`).
   - `ThreadRole.MANUAL_CASE` (`machine_setup/thread_role.py`).
   - Exceptions (`machine_setup/exceptions.py`
     `ManualChatSendRefusedError`).
   - Policy strings (`projects/manual_run_policy.py`).
   - Runner forms (`projects/forms.py`: `ManualPauseForm`,
     `RunnerManualProgressForm`, `RunnerManualChatSendForm`).
   - Freeze the service signatures named above.
1. **Part A, projects server: pause and progress** (parallel). Files:
   - `projects/manual_case_pause_services.py`,
     `projects/manual_case_progress_services.py`
   - `projects/manual_case_claim_services.py` (write the cleared pause
     fields)
   - `projects/manual_case_submit_services.py` (docstring)
   - `projects/runner_manual_services.py`,
     `projects/runner_manual_state_services.py`,
     `projects/runner_case_services.py`
   - `projects/services.py` (event fields only)
2. **Part B, projects web** (parallel). Files:
   - `projects/manual_case_views.py`, `projects/urls.py`
   - `projects/manual_case_display_services.py`
   - `templates/projects/includes/_manual_case_card.html`,
     `templates/projects/test_run_case_detail.html`,
     `templates/projects/test_run_detail.html`
   - `static/css/punk-hazard.css` (if a badge is needed)

   It consumes Part A's services and the `ManualPausePanel` from the
   foundation's frozen signatures.
3. **Part C, machine_setup discussion** (parallel). Files:
   - `machine_setup/services/manual_case_context.py`, `open_manual_chat.py`,
     `manual_chat_tools.py`
   - `setup_tools.py`, `setup_prompt.py`, `run_turn.py`, `send_gates.py`,
     `send_message.py`, `runner_discussion.py`, `chat_page.py`
   - `machine_setup/views.py`, `machine_setup/forms.py`,
     `machine_setup/urls.py`, the thread template
4. **Part D, Test Runner** (parallel). Files:
   - `controller_client/person_window_manual.py`, new
     `person_window_chat.py`, `person_window_app.py` (extract only)
   - `person_window_ipc.py`, `manual_coordinator.py`, `client.py`
   - `person_window_diagnostics.py` (sample snapshot with paused and
     discussion)
5. **Integration** (orchestrator):
   - `projects/controller_consumer.py` (new handlers and capability gate)
     and `projects/runner_window_state.py` (compose `active.discussion`).
   - Wire the URLs.
   - `pre-commit run --all-files`, `manage.py check`,
     `makemigrations --check`, controller mypy (including the win32
     platform).
6. **Docs**: `docs/changed/2026-11-01-manual-pause-and-discuss.md`, the
   CLAUDE.md Manual bullet (Pause/Discuss, CLIENT_VERSION 0.16.0,
   capability), and plan Status Done.

## Verification

No automated tests (project rule).

1. Static checks:
   - `pre-commit run --all-files`
   - `python manage.py check`
   - `python manage.py makemigrations --check`
   - `sqlmigrate projects 0055` and `sqlmigrate machine_setup 0011`
   - controller mypy, including `--platform win32`
2. Throwaway-DB smoke (`manage.py shell`):
   - Claim → pause → continue → submit: `active_duration` excludes the
     paused span, and the pause fields clear at hand-in.
   - Pause → abort, pause → re-test mark, pause → web release: the pause
     clears.
   - Double pause is a no-op.
   - Continue while not paused is refused.
   - A wrong claim token is refused.
3. Progress: record with token A, then re-claim (token B). `load_manual_progress`
   returns None. Oversized and invalid step numbers are refused.
4. Send gates:
   - With a STARTED manual run, a `manual_case` thread send passes, and the
     main chat and a result chat are still refused.
   - After hand-in, a manual thread send is refused with the hand-in
     message.
   - `run_setup_task` in the role is refused while not paused, and allowed
     (machine READY) while paused.
5. Snapshot: query counts constant (one more with a claim, about three more
   with a discussion). A runner without the capability gets a byte-identical
   snapshot to today's (compare serialized output).
6. Runner (`python -m controller_client.diagnose --window --no-interactive`
   plus a live check):
   - Pause disables marks, Continue re-enables them.
   - Discuss shows the agent reply.
   - A restart while paused restores Paused from the snapshot.
   - The window is hidden during an agent sub-task.
7. Browser:
   - Case page Pause / Continue / Discuss / Go to discussion, and the
     "Paused by" badge live on the run page.
   - A read-only member gets 403 on the POSTs.
   - The agent "Pause and discuss" is still absent on manual cases.
   - The runner `cases` list no longer shows the manual case as running.
8. Run `reviewer` and `verifier` in parallel on the combined diff.

## Open questions

Resolved 2026-11-01: the user chose the stated assumption for every question
(Q1 (b), Q2 both, Q3 claimed only, Q4 runner reports, Q5 no expiry, Q6
separate buttons, Q7 exclude paused time, Q8 two threads).

- Q1. **Machine access in the manual discussion.** Should the agent be able
  to act on the test machine (`run_setup_task`)? Options:
  - (a) never, talk and KB only;
  - (b) only while the case is paused;
  - (c) always, even while the tester works.

  *Assumption: (b). The check is at call time, and Continue is refused while
  the agent's machine sub-task runs.*
- Q2. **Who can Pause / Continue.** Options:
  - only the tester at the Test Runner;
  - also a writable member on the web case page (for example, a tester who
    walked away without pausing).

  *Assumption: both, runner and web.*
- Q3. **When Discuss is available.** Options:
  - only for the claimed case, paused or not, read-only after hand-in or
    release (Discuss result takes over after hand-in);
  - also for an unclaimed manual case, to talk it through before starting.

  *Assumption: claimed only.*
- Q4. **Telling the AI the steps marked so far.** Options:
  - the runner reports them to the server (debounced `manual_case_progress`),
    and the web card shows them at page load;
  - the agent knows only what the tester writes in the chat.

  *Assumption: the runner reports them. No live per-mark update on the web.*
- Q5. **Long pauses.** Should a pause expire (auto-release, or notify the
  tester) after some hours? *Assumption: no expiry. A writable member can
  still Release a stuck claim from the web.*
- Q6. **Buttons.** Options:
  - separate "Pause" / "Continue testing" and "Discuss";
  - one "Pause and discuss" that also opens the discussion with a first
    message, as in agent modes.

  *Assumption: separate buttons. A break needs no discussion, and Discuss
  must work without pausing.*
- Q7. **Paused time and TestRail.** Should paused time be left out of the
  case's measured duration (and TestRail "elapsed")? *Assumption: yes, it is
  left out.*
- Q8. **One thread or two per case.** Should the manual discussion carry on
  as the result chat after hand-in, so there is one thread per case?
  *Assumption: no, two separate threads. The manual thread becomes
  read-only, and "Discuss result" opens the existing result chat.*

## Changelog

- 2026-11-01: created (Draft).
- 2026-11-01: open questions resolved as assumed; In progress.
- 2026-11-01: integration: `open_manual_chat(claim_token=)` re-checks a runner
  send's claim token under the locks.
- 2026-11-01: review pass 1. Fixed: Release, hand-in (retryable refusal) and
  claiming another case refused while a manual discussion turn is live; one
  rule `manual_run_policy.is_claimed_manual_case`; Continue shown disabled
  while a turn is live (web and window); progress throttle 60/min; fewer
  queries per runner push (`live_turn_manual_case_ids`);
  `send_manual_from_runner(project=)`; snapshot size asserts including the
  discussion (`MANUAL_DISCUSSION_SNAPSHOT_MAX_BYTES`); nits.
  Rejected: parsing the checkpoint per progress report (≤ 60/min per project,
  one row; caching the step count would need another column for little gain).
  Kept: the manual discussion's "reply ready" ntfy to an away sender (the
  non-goal covers Need help / Failed pushes only). Kept: a progress report
  counts as sent once written; the higher throttle makes a refusal rare and
  the next mark change resends it. Kept: the case page does not reload when a
  discussion turn ends (Continue stays disabled until a reload; the server
  check is authoritative).
- 2026-11-01: review pass 2. Accepted without change: `send_setup_message`
  re-checks only that the case is claimed, not the token; a release and
  re-claim by the one owner runner between the two transactions would post
  to the same case's thread (cosmetic).
- 2026-11-01: review and verification clean; documented in
  `docs/changed/2026-11-01-manual-pause-and-discuss.md`; Done.
