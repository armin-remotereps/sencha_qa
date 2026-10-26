# refactor: Only a person marks a case Blocked; faults ask for help

Status: Done
Created: 2026-10-26
Request: Today a case worker sets a case to Blocked on its own whenever an
infrastructure fault stops it (runner lost or unresponsive, visual engine, AI
service, browser missing, runner outdated, restore failed, soft time limit,
internal error). The request is that neither the agent nor the worker may ever
put a case in Blocked. When something stops a case, the case asks for help
(Need help: the run pauses and people are notified, per the run's help mode) and
says why. Blocked is set only by a person who looks at the case and decides it
cannot be fixed now. The existing "Skip" action becomes that explicit
"Mark blocked" action. Type: `refactor` (the visible behaviour changes, but no new
capability is added: two existing flows are rewired).

## Context

- **Automatic Blocked, the only worker-side writer.** In
  `projects/services.py::execute_test_run_test_case`, the
  `except ControllerReconnectTimeoutError` branch calls
  `_abort_run_after_reconnect_timeout`, which runs `_mark_pivot_blocked(RUNNER_DISCONNECTED)`
  and then `abort_test_run`, so the remaining cases are Cancelled and the run is
  Cancelled. The `except Exception` and `except BaseException` branches call
  `_settle_pivot_after_error`, which runs `_mark_pivot_blocked` with
  `projects/case_blocking.py::classify_case_exception` + `blocked_message`. These
  are the only automatic writers of `TestRunTestCaseStatus.BLOCKED`; I checked this
  with a grep over every `.py` outside migrations. `agents/` and `machine_setup/`
  never set Blocked. The orchestrator can only decide `need_help`, and the help-chat
  agent can only *propose* `skip`
  (`machine_setup/services/help_chat_tools.py`, `HelpProposalAction.SKIP`).
- **Human Blocked.** `projects/case_help_services.py::skip_after_help` sets
  `BLOCKED` + `BlockedReason.HELP_SKIPPED` and resolves the request
  `HelpResolution.SKIPPED`. Its callers are the view `projects/views.py::test_run_case_help_skip`,
  the URL `projects:test_run_case_help_skip` (`.../help/skip/`), and the buttons in
  `templates/projects/includes/_case_help_card.html` and
  `templates/machine_setup/includes/_help_panel.html`. Help text mentions Skip in
  `templates/machine_setup/chat.html`.
- **Need help path to reuse.** `projects/services.py::_pause_run_for_help(pivot,
  HelpRequestInfo, generation)` runs under the run lock and does the following:
  it creates `CaseHelpRequest(step_num, reason, ask)`, sets the case to `NEED_HELP`
  and the run to `PAUSED`, and logs the request. After commit it either enqueues
  the Night auto-fix (`auto_fix_eligible`, `projects/case_help_policy.py`) or calls
  `notify_case_needs_help`. A paused case gets no teardown and no cleanup.
  Leftover chain tasks stand down on `PAUSED`.
- **Continue.** `continue_after_help` writes the checkpoint `help` key
  (`agents/checkpoint.py::with_help`, `CheckpointHelp.continue_pending=True`). The
  worker then skips the pre-case cleanup, and the orchestrator skips the restore
  sub-task and re-runs the cursor step from the current screen.
  `list_reopen_blockers` gates Continue: the runner must be connected and the visual
  engine ready (`projects/run_reopen_services.py`).
  `agents/services/orchestrator.py::_rebuild_state` writes the help line only on the
  *failed attempt* the case paused at (`_help_anchor_index`).
- **Night auto-fix and how it falls back to a person today.** I read every path:
  - The worker cannot enqueue the start task (`projects/case_help_state.py::enqueue_case_auto_fix`):
    the request is set to not fixed and people are notified at once.
  - The start task refuses (`machine_setup/services/case_auto_fix.py::_start_locked`)
    because another setup turn is live, or the machine is not READY (runner gone,
    visual engine offline): `finish_case_auto_fix(fixed=False)`, notified at once.
  - The turn raises, for example an `AIServiceError` from the LLM client
    (`machine_setup/services/run_turn.py`, `run()` catches `Exception`):
    `_finish` → `settle_case_auto_fix` → not fixed → notified at once.
  - The turn ends without `report_case_fix`, or with `fixed=false`
    (`auto_fix_settlement._fix_outcome`): not fixed, notified at once.
  - The worker is killed, the start task is lost, or a settle raises:
    `sweep_stale_auto_fixes` (beat, every 120 s) settles the request as not fixed.
    A lost start is settled after `AUTO_FIX_START_GRACE` (15 min), a dead turn after
    `SETUP_TURN_STALE_AFTER` (turn timeout + 420 s), and an unsettled ended turn
    after 2 min.
  - Q10 (`auto_fix_eligible`): a step that fails again after a reported fix goes
    straight to a person.
- **Auto-fix brief and prompt.** The pure `machine_setup/case_help_brief.py::_auto_fix_brief`
  is filled by `machine_setup/services/case_help_context.py`. The prompt section is
  `machine_setup/services/setup_prompt.py::_build_auto_fix_thread`. Both assume the
  tester asked for help, and the brief says the step "re-runs from the current
  screen".
- **Resume / Re-run blocked** (`projects/run_resume_services.py`,
  `projects/run_rerun_services.py`) act on Blocked and Cancelled cases of a
  Done/Cancelled run. **Teardown policy** (`projects/case_teardown_policy.py`) tears
  down Failed cases and Blocked cases with some worker-set reasons.
- **Blocked consumers that stay as they are**: run and dashboard counts
  (`projects/services.py` annotations, `dashboard/services.py`), the TestRail
  mapping (`projects/testrail_mapping.py`, `testrail_results_services.py`: Blocked →
  status 2, comment "<reason label>: <result>"), the Checked column
  (`run_case_check_services.py`), the step progress (`case_step_progress.py`) and
  the WebSocket `blocked_reason` fields.
- History: `docs/changed/2026-10-22-blocked-result.md`,
  `2026-10-22-resume-blocked-case.md`, `2026-10-24-human-in-the-loop.md`,
  `2026-10-24-help-chat.md`. Versions: Django ~=5.2.11, Celery ~=5.6.2,
  Channels ~=4.3.2 (`requirements.txt`). The plan relies on no new library
  behaviour.

## Goals

- No code path outside a person's request sets a case to `BLOCKED`.
- Every fault that today blocks a case instead pauses the run with a Need help
  request. The request records which fault it was (`fault`) and shows a safe,
  server-built reason and ask.
- In Night mode every help request goes to the auto-fix agent first, faults
  included (subject to Q10). The agent decides from the fault in its brief whether
  it can fix it. When the auto-fix cannot run or cannot fix, a person is notified
  at once, with the beat sweep as the backstop. No request stays stuck in auto-fix.
- "Skip" becomes **Mark blocked**: in the UI, in the service name, in the URL and
  in the help-chat proposal wording. It keeps its behaviour: the case is Blocked
  with the generic reason "Marked blocked by a person" and can be resumed later,
  and the run continues.
- Continue works for a fault request. If the case never reached a checkpoint, it
  plans from scratch. If the runner was lost, the earlier steps' state is restored
  first. Otherwise the interrupted step runs again from the current screen.
- Existing Blocked rows, Resume run, Re-run blocked, TestRail, counts and
  dashboards keep working with no data migration.

## Non-goals

- Automatic retry or backoff of transient faults before asking (Q7: no retries in
  this change).
- A static list of which faults the auto-fix may try (Q1: the agent decides).
- New controller actions or a protocol change.
- A Celery hard time-limit kill still leaves the case In progress (a known
  residual from the blocked-result note).
- Changing Abort, Force disconnect or broker-refusal cancellation: they still
  cancel cases, and cancelling is not blocking.
- Changing Failed. Runner `EXECUTION_FAILED` errors and command timeouts stay
  Failed as decided in `feat-blocked-result.md`.

## Design

### Data model

- `CaseHelpRequest.fault = CharField(max_length=32, choices=BlockedReason.choices,
  blank=True, default="")`. It is empty when the orchestrator asked for help and set
  when a fault paused the case. I reuse `BlockedReason` rather than adding a parallel
  enum with the same values: it already means "why a case could not be executed or
  judged". Its docstring gets updated to say it also classifies help requests.
- Label-only changes, so no SQL; Django still emits `AlterField`:
  - `BlockedReason.HELP_SKIPPED` → "Marked blocked by a person";
  - `HelpResolution.SKIPPED` → "Marked blocked";
  - `HelpProposalAction.SKIP` → "Mark blocked".
  - The stored values (`help_skipped`, `skipped`, `skip`) stay, so old rows keep
    rendering (Q5).
- The `TestRunTestCase.blocked_reason` field and all `BlockedReason` values stay,
  so old rows still show their reason. New Blocked rows always get `help_skipped`
  (Q4); the other values are only written to `CaseHelpRequest.fault` from now on.
- `agents/types.py::CheckpointHelp` gets an optional `fault: str = ""` (the fault
  label, for the agent's state line), serialized as an optional key in
  `agents/checkpoint.py`. This follows the pattern of `ask` and `auto_fix_summary`,
  so `CHECKPOINT_VERSION` does not change.
- Migration `projects/0031_help_fault` (additive): `AddField` plus the three choice
  `AlterField`s. Rollback: drop the field. No data step is needed.
- No new exception classes. The fault exceptions already exist in
  `projects/exceptions.py` and `agents/exceptions.py`, and the help actions keep
  raising `RunHelpError`.

### Services

- **`projects/case_blocking.py` → renamed `projects/case_faults.py`** (pure, no
  ORM). The name "blocking" would mislead once a fault no longer blocks. Its only
  importer is `projects/services.py`.
  - `classify_case_fault(exc: BaseException) -> BlockedReason`, renamed from
    `classify_case_exception`, with an unchanged table.
  - `fault_message(exc: BaseException, reason: BlockedReason) -> str`, renamed from
    `blocked_message`, with the same safe-text rules.
  - New `fault_ask(reason: BlockedReason) -> str`: fixed server text per fault,
    telling a person or the setup agent what to fix. Examples: "Reconnect the Test
    Runner, then Continue." "Start or check a vision worker, then Continue."
    "Update the Test Runner on the test machine, then Continue."
  - New `class HelpContinueMode(StrEnum)`: `CURRENT_SCREEN`, `RESTORE`,
    `FROM_SCRATCH`.
  - New `help_continue_mode(fault: str, has_checkpoint: bool) -> HelpContinueMode`
    (Q2). It returns `FROM_SCRATCH` without a usable checkpoint, `RESTORE` for
    `runner_disconnected` and `runner_unresponsive`, and `CURRENT_SCREEN` otherwise.
    It is the single rule that the services, the auto-fix brief and the templates
    all read.
- **`projects/case_help_policy.py`**: unchanged. Q10 eligibility applies to fault
  requests too. That bounds the loop "the agent reports fixed → the same fault comes
  back": the second time goes to a person.
- **`projects/services.py`**:
  - `_pause_run_for_help(pivot, help, generation, *, fault: BlockedReason | None =
    None)` writes `fault` on the request. The Night decision is unchanged
    (`help_mode == NIGHT and auto_fix_eligible`) and holds for every request, faults
    included (Q1). For a fault the log line reads
    `[Need help: <label> at step N] <ask>`.
  - New `_pause_case_after_fault(pivot, exc, generation) -> bool`. It replaces
    `_settle_pivot_after_error` and keeps the same guard: a case no longer In
    progress is left alone, and a cancelled run cancels the case. It classifies the
    exception and logs it the same way as today (internal error with traceback).
    It builds a `HelpRequestInfo`:
    - `step_num` is the checkpoint's `next_step`, or 1 without a checkpoint;
    - `reason` is `fault_message`;
    - `ask` is `fault_ask`.
    It then calls `_pause_run_for_help(..., fault=reason)`. It returns whether the
    case paused.
  - `execute_test_run_test_case`:
    - The `ControllerReconnectTimeoutError` branch (Q3), the `Exception` branch and
      the `BaseException` branch (Q6, classified as `internal_error`) all call
      `_pause_case_after_fault` and set `paused_for_help` from its result, so the
      `finally` skips teardown and cleanup.
    - The BaseException branch still re-raises.
    - `_abort_run_after_reconnect_timeout`, `_settle_pivot_after_error` and
      `_mark_pivot_blocked` are deleted. After this change no worker-side writer of
      `BLOCKED` remains.
- **`projects/case_help_services.py`**:
  - `skip_after_help` → `mark_blocked_after_help`, with the same locking and
    resolution (`HelpResolution.SKIPPED`). It always sets
    `blocked_reason = BlockedReason.HELP_SKIPPED` (Q4b).
  - The fault appears only in the result text. The headline is "Marked blocked by
    <user> at step N while it was waiting for help (<fault label>).", with the
    parenthesis only when a fault is set. Then come the ask and the note via
    `_verdict_text`. TestRail therefore gets "Marked blocked by a person: Marked
    blocked by … (Test Runner disconnected). …".
  - `HelpPanel` gains `continue_mode: HelpContinueMode`, computed in
    `get_help_panel` from `request.fault` and whether
    `request.test_run_test_case.execution_checkpoint` parses (that column is already
    loaded). Both the case card and the thread panel read it.
  - `_set_continue_pending` (Continue, and the auto-fix reporting fixed) branches on
    `help_continue_mode`:
    - `FROM_SCRATCH`: clear the checkpoint to `{}` and write no help key, so the
      case plans from scratch with the normal cleanup. Today's code would write a
      lone `help` key, so the worker would skip cleanup and then plan from scratch
      anyway.
    - `RESTORE`: write the help key with `continue_pending=False`. The worker then
      takes the normal resume path: cleanup and a restore sub-task before the cursor
      step. The help line still reaches the agent's state.
    - `CURRENT_SCREEN`: as today (`continue_pending=True`).
    In every mode the help key carries `fault` (its label).
  - Flash text: "Marked the case blocked".
- **`agents/services/orchestrator.py`**: `_rebuild_state` adds a fallback. When
  `help` is set but `_help_anchor_index` finds no failed attempt at
  `help.step_num` (a fault stopped the step mid-run, so no attempt was recorded),
  it appends the help state line for that step. `format_help_state_line` in
  `orchestrator_prompts.py` names the fault when `help.fault` is set ("Step N was
  interrupted (<fault>); the test machine was fixed by …"). The live-continue path
  in `_execute_sub_tasks` gets the same fallback. The implementer must read both
  paths: the anchor logic is the part of this plan I am least sure of.
- **`projects/case_teardown_policy.py`**: workers no longer produce Blocked, so the
  Blocked branch and `TEARDOWN_BLOCKED_REASONS` are dead code. Reduce
  `needs_teardown` to `status == FAILED` and update the docstring. A case a person
  marks Blocked or Failed after help gets no teardown, as Mark failed already works
  today.
- **Auto-fix for faults (Q1)**, in `machine_setup`:
  - `machine_setup/case_help_brief.py`: `CaseHelpBriefData` gains `fault_label: str`
    and `continue_mode: str`.
    - `_auto_fix_brief`, for a fault, opens with "This case was not stopped by the
      tester. An infrastructure fault interrupted step N: <label>. <reason>", and
      shows the ask.
    - Its "after you report fixed" sentence follows `continue_mode`: "re-runs step N
      from the current screen", "first restores the earlier steps' state, then runs
      step N", or "starts the case again from the beginning".
    - `_person_brief` shows the fault label too.
  - `machine_setup/services/case_help_context.py` fills the new fields (fault
    display, plus `help_continue_mode` from the loaded checkpoint).
  - `machine_setup/services/setup_prompt.py::_build_auto_fix_thread` gets an
    INFRASTRUCTURE FAULTS paragraph, as guidance with no new limits:
    - Decide whether the cause is on this test machine and within your reach.
    - Something running proves itself: the turn could start only with the runner
      connected and the visual engine ready, and you are answering, so the AI
      service works. For a fault that may have been transient (runner unresponsive,
      visual engine, AI service, time limit), check the screen and the step's
      preconditions. If the machine is fine, report `fixed=true` and say it looked
      transient.
    - Install a missing browser. Bring the app to the state the step needs when the
      restore failed.
    - Report `fixed=false` with what a person must do when the cause is outside the
      machine or would end this session. Examples: a server internal error,
      updating or restarting the Test Runner itself, a vision worker on another
      machine that keeps failing.
    - Never stop or restart the Test Runner process.
  - `machine_setup/services/help_chat_tools.py`: the `propose_resolution`
    description reads "skip: mark the case Blocked (a person decides; it can be
    resumed later)". The enum value stays `skip`.
  - `machine_setup/services/help_thread_state.py::help_panel_payload` adds
    `fault_label` and `continue_mode` to the thread panel's socket payload.
- **Fallback when the auto-fix cannot run.** The existing paths listed under
  Context already hand off to a person right away, with the sweep as backstop. Faults
  add no new stuck state:
  - Runner gone or visual engine offline: refused at start → person at once.
  - AI service down: the turn's first LLM call raises → the turn fails → settled not
    fixed → person at once.
  - Worker killed or start lost: the sweep settles it.
  The plan adds two guards:
  1. `start_case_auto_fix`'s refusal summary names the fault ("Auto-fix could not
     start: the test machine was not ready …"). This already happens through
     `_START_REFUSED_PREFIX`; check that the notification is the "Auto-fix could not
     fix it" one.
  2. In verification, prove each path ends with the request `not_fixed` and a
     notification sent: AI key revoked, runner stopped, worker killed mid-turn. No
     new code is expected; if a path does not settle, fix it in
     `auto_fix_settlement.py` or the sweep, not with a fault allowlist.

### Pages and forms

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `projects:test_run_case_help_block` → `/projects/<id>/test-runs/<run_id>/cases/<pivot_id>/help/block/` (replaces `..._help_skip`, `help/skip/`; the old URL is dropped, no redirect) | POST | `@project_membership_required` | `CaseHelpResolutionForm` (unchanged fields) | redirects to the case page or the help thread (`_help_return_redirect`) | `RunHelpError` → flash error |
| Case page help card (`_case_help_card.html`) | GET | as today | as today | new: a fault badge (`help_request.get_fault_display`) above the reason when `help_request.fault`; button "Mark blocked"; the Continue hint follows `help_panel.continue_mode` (re-runs step N from the current screen / restores the earlier steps first / starts the case again); "Mark blocked leaves the case Blocked so it can be resumed later" | — |
| Help thread panel (`machine_setup/includes/_help_panel.html`, `chat.html` hint strings) | GET + socket | as today | as today | "Mark blocked" button and wording; fault badge; the hint from the payload's `continue_mode` | — |
| Run page (`test_run_detail.html`) | GET | as today | — | the help banner shows `help_panel.request.get_fault_display` when set (server-rendered; there is no live socket field, since `RunHelpState` is a state enum, not a payload). The Reason column for `need_help` shows the fault label when set. The Abort dialog is unchanged | — |
| Case page "Why this case was blocked" card | GET | as today | — | the hint becomes "A person marked this case blocked …" for `help_skipped`. Older rows with worker-set reasons keep the current text | — |

All text stays auto-escaped. `fault_message` keeps the existing rule: visual-engine
and internal errors show fixed text, never exception text.

### Background work and WebSockets

- No new Celery task, queue or beat entry. The fault path uses the existing
  auto-fix start task or the `notify`-queue `send_notification`. The sweep stays the
  backstop.
- WebSocket: no new event type. The setup thread `help` payload gains `fault_label`
  and `continue_mode`. The run and case events are unchanged, and `blocked_reason`
  stays empty for Need help.
- Controller protocol unchanged; `CLIENT_VERSION` and capabilities are untouched.
  `runner_outdated` now asks for help instead of blocking.

### Settings / infrastructure

None.

### Data flow

1. A tool raises an infrastructure error, the soft time limit fires, the worker
   shuts down, or `wait_for_controller`'s grace period expires.
   `execute_test_run_test_case` catches it, unless the execution is stale.
2. `_pause_case_after_fault` classifies the fault and builds a safe reason and ask.
   `_pause_run_for_help` locks the run and opens a `CaseHelpRequest(fault=…)`. The
   case becomes Need help and the run Paused. The machine is left as it is.
3. After commit, the page is broadcast. Then:
   - **Attended**, or Night but not Q10-eligible: ntfy to project members.
   - **Night**: the auto-fix starts with a brief that names the fault. If it cannot
     start or run, a person is notified at once (the sweep is the backstop). If it
     reports not fixed, a person is notified. If it reports fixed, the case
     continues per `continue_mode`.
4. A person uses one of three actions on the case page or in the help chat:
   - **Continue**: gated on the runner and the visual engine. It runs per
     `continue_mode`: plan from scratch, restore then the cursor step, or the
     cursor step from the current screen.
   - **Mark failed**.
   - **Mark blocked**: Blocked, `help_skipped`, with the fault in the result text.
   After any of these the rest of the run is queued.
5. Abort cancels everything, as today.

## Alternatives considered

- **Keep Skip and add a separate "Mark blocked".** Rejected. Both would leave the
  case Blocked and resumable, so two buttons with one outcome only add confusion.
- **A new `HelpFault` enum instead of reusing `BlockedReason`.** Rejected. It would
  duplicate the same nine values.
- **Copy the fault into `blocked_reason` on Mark blocked.** Rejected by the user
  (Q4b). Blocked now always means a person decided, and the fault stays visible in
  the text.
- **A static allowlist of auto-fixable faults.** Rejected by the user (Q1). The
  agent sees the fault and decides; the existing refusal and failure paths cover the
  faults where it cannot even start.
- **Rename the stored values (`skipped` → `marked_blocked`, …).** Rejected. That
  needs a data migration on populated tables for a label-only gain.
- **Keep cancelling the run when the runner is lost past grace.** Rejected (Q3).
  Pausing keeps the remaining cases Created, so they continue once the runner is
  back.
- **Let the person pick restore or no-restore on Continue (a checkbox).** Not
  chosen: the per-fault rule (Q2) is simpler. It can be added later.

## Risks and mitigations

- **One transient blip now pauses a whole night run.** Before, a visual-engine
  or AI blip blocked a single case and the run went on. That is the accepted
  trade-off (Q7: no retries). Mitigation: in Night mode the auto-fix runs first.
  It may find the machine fine and report fixed, so the run continues on its own,
  bounded by Q10.
- **Auto-fix reports fixed for a fault it cannot fix** (for example a server
  internal error). The case re-runs, hits the fault again, and Q10 sends it to a
  person. The cost is one extra attempt; the prompt guidance makes it rare.
- **Auto-fix restarting the Test Runner.** It would cut its own session, and the
  case would then stay paused. The prompt forbids it. If it happens anyway, the turn
  fails and is settled not fixed, so a person is notified.
- **AI outage.** The auto-fix turn fails at its first LLM call and is settled not
  fixed within seconds. The help chat cannot answer, but Continue, Mark failed and
  Mark blocked do not need the AI.
- **Soft time limit.** About 60 s remain before the hard kill. The pause is a short
  transaction, and a paused case skips teardown and cleanup, so it finishes faster
  than today's block plus teardown.
- **BaseException (worker shutdown).** The pause transaction commits inside the
  handler, so its `on_commit` callbacks (notification or auto-fix enqueue) run
  before the re-raise. If the process dies before commit, the case stays In
  progress (the existing residual).
- **Continue on a mid-step interruption.** The interrupted step may be half done
  (text half typed). Re-running it from the current screen is what Continue already
  does after a failed step, and the help line tells the agent to check the screen.
- **Checkpoint compatibility.** `fault` is an optional key, so old checkpoints
  parse unchanged. A worker on the old code ignores the key, because `_parse_help`
  reads only the keys it knows (confirmed).
- **Rolling deploy.** An old execution worker still running during deploy can
  still block a case once. That is harmless: the state stays valid and resumable.
- **Old open pages posting to `help/skip/`** get a 404 (accepted, Q5).
- **Docs drift.** `CLAUDE.md` still says "a case needing it on an older runner is
  Blocked `runner_outdated`". Update it in the documentation step.

## Implementation steps

### Phase 0: shared code, written first by the orchestrating agent

Every later part builds on these files, so no sub-agent edits them.

1. `projects/models.py`: `CaseHelpRequest.fault`, the three label changes, and the
   `BlockedReason` docstring.
2. `projects/migrations/0031_help_fault.py`: generate it with `makemigrations` and
   review it. It must contain only `AddField` and the `AlterField`s.
3. `agents/types.py` (`CheckpointHelp.fault: str = ""`) and `agents/checkpoint.py`
   (`help_to_json` / `_parse_help`).
4. `projects/case_faults.py`, which replaces `projects/case_blocking.py` (delete
   the old file): `classify_case_fault`, `fault_message`, `fault_ask`,
   `HelpContinueMode` and `help_continue_mode`.

Phase 0 leaves `projects/services.py` importing the deleted module until Part A
lands; the tree only has to type-check at integration. No new exception classes are
needed.

### Phase 1: five parallel sub-agents, each owning disjoint files

| Part | Owns (only these files) | Consumes | Exposes |
|---|---|---|---|
| **A. Worker path** | `projects/services.py`, `projects/case_teardown_policy.py` | `case_faults.*`, `CaseHelpRequest.fault` | `_pause_run_for_help(..., fault=)`, `_pause_case_after_fault`; the three except branches rewired; the blocking helpers deleted; the import switched to `case_faults` |
| **B. Human actions** | `projects/case_help_services.py`, `projects/views.py`, `projects/urls.py`, `projects/forms.py` (docstring) | `help_continue_mode`, `CheckpointHelp.fault` | `mark_blocked_after_help`; `HelpPanel.continue_mode`; the URL name `projects:test_run_case_help_block` (path `help/block/`); the view `test_run_case_help_block`; the `_set_continue_pending` modes |
| **C. Orchestrator** | `agents/services/orchestrator.py`, `agents/services/orchestrator_prompts.py`, plus the comment-only edits in `agents/services/agent_loop.py`, `tool_utils.py`, `tools_controller.py` | `CheckpointHelp.fault` | the help-line fallback in `_rebuild_state` and the live-continue path |
| **D. Machine setup** | `machine_setup/case_help_brief.py`, `machine_setup/services/case_help_context.py`, `machine_setup/services/setup_prompt.py`, `machine_setup/services/help_chat_tools.py`, `machine_setup/services/help_thread_state.py` | `help_continue_mode`, `CaseHelpRequest.fault` | the brief fields `fault_label` and `continue_mode`; the socket payload keys `fault_label` and `continue_mode` (strings) |
| **E. Templates** | `templates/projects/includes/_case_help_card.html`, `templates/machine_setup/includes/_help_panel.html`, `templates/machine_setup/chat.html`, `templates/projects/test_run_detail.html`, `templates/projects/test_run_case_detail.html` | the context keys `help_request.fault` / `get_fault_display`, `help_panel.continue_mode` (values `current_screen` / `restore` / `from_scratch`), the URL name `projects:test_run_case_help_block`, the payload keys `fault_label` / `continue_mode` | — |

No two parts share a file. E needs no view change: everything it reads comes from
`help_panel`, `help_request` or the socket payload, which B and D provide.

### Phase 2: integration and documentation (orchestrating agent)

- Read the combined diff. Check the seams:
  - every `skip_after_help` / `test_run_case_help_skip` reference is gone;
  - the `case_faults` imports;
  - the `HelpContinueMode` values used in the templates match the enum.
- Run `pre-commit run --all-files`, `python manage.py check` and
  `python manage.py makemigrations --check`.
- Update the `CLAUDE.md` lines on Need help and the `runner_outdated` capability.
- Write `docs/changed/<date>-no-automatic-blocked.md` and set this plan to Done.

## Verification

- `pre-commit run --all-files` (strict mypy, black, isort), `python manage.py check`,
  `python manage.py makemigrations --check`.
- `grep -rn "TestRunTestCaseStatus.BLOCKED" --include='*.py'` must show exactly
  one writer: `mark_blocked_after_help`. The rest are reads and filters.
  `grep -rn "case_blocking\|skip_after_help\|help_skip"` must return nothing in
  code or templates.
- Manual, with a live runner and a vision worker:
  1. Attended run: stop the vision worker mid-case. The case goes Need help with the
     "Visual engine unavailable" badge, the run is Paused and an ntfy notification
     arrives. Start the worker and press Continue. The case re-runs the interrupted
     step from the current screen, and the log's state shows the "interrupted" help
     line.
  2. Kill the Test Runner past the grace period. The run pauses instead of being
     cancelled, and the remaining cases stay Not started. Continue stays disabled
     with "Connect the Test Runner…". Reconnect and Continue: the log shows the
     cleanup and a restore sub-task, then the step.
  3. Night run, browser not installed: the auto-fix starts with a brief naming the
     fault. It installs the browser, reports fixed, and the case continues on its
     own.
  4. Night run, runner stopped past grace: the auto-fix start is refused and a
     person is notified at once ("Auto-fix could not fix it"). The request is
     `not_fixed`, not `running`.
  5. Night run, LLM key revoked mid-case: the case pauses with `ai_service`. The
     auto-fix turn fails on its first LLM call, and the request is `not_fixed` and
     notified within seconds.
  6. Night run, kill the machine-setup worker mid auto-fix: within
     `SETUP_TURN_STALE_AFTER` plus one sweep, the request is `not_fixed` and
     notified.
  7. Night run, the same fault twice at the same step after a fixed report: the
     second time goes straight to a person (Q10).
  8. A fault during planning (bad LLM key on the first case): Continue after
     fixing the key plans from scratch, with cleanup. The card's hint says "starts
     the case again".
  9. Mark blocked on a fault request: the case is Blocked "Marked blocked by a
     person", and the result text names the fault. The run continues, and Resume run
     and Re-run blocked both pick the case up. TestRail gets status 2 with "Marked
     blocked by a person: Marked blocked by … (<fault>) …".
  10. Soft time limit (temporarily lower `soft_time_limit` of
      `execute_test_run_case` in `projects/tasks.py` in dev, then revert): the case
      goes Need help with `time_limit` well before the hard kill.
  11. Old data: a run with pre-existing Blocked rows (any reason) renders the run
      page, case page, dashboard and resume panel unchanged.
  12. The help chat's `propose_resolution` with `skip` shows "Mark blocked" as the
      suggestion.
- Run `reviewer` and `verifier` in parallel on the diff, giving the verifier the
  original request and this plan.

## Open questions

All resolved on 2026-10-26:

- Q1. Which faults may Night mode hand to the auto-fix? **Resolved: all of them;
  no static list.** The agent gets the fault label, reason and ask in its brief,
  decides, and reports `fixed=false` when it cannot fix. The existing refusal,
  turn-failure and sweep paths hand off to a person when it cannot run. The prompt
  covers infrastructure faults.
- Q2. Restore or current screen on Continue? **Resolved: as assumed.** Restore,
  with the normal cleanup, only for `runner_disconnected` and `runner_unresponsive`.
  Every other fault uses the current screen, and a case without a checkpoint plans
  from scratch.
- Q3. Runner lost past grace? **Resolved: pause for help.**
- Q4. Reason on Mark blocked? **Resolved: (b)**, always "Marked blocked by a
  person" (`help_skipped`). The fault appears only in the result text and the
  TestRail comment.
- Q5. Rename Skip to Mark blocked everywhere, keep the stored values, drop the old
  URL? **Resolved: yes.**
- Q6. Worker shutdown mid-case? **Resolved: ask for help as `internal_error`.**
- Q7. Retries before pausing? **Resolved: no retries in this change.**

## Changelog

- 2026-10-26: created (Draft).
- 2026-10-26: user answered Q1–Q7. Status changed to Ready.
  - Q1: dropped the static fault allowlist (`fault_auto_fixable` removed). Every
    Night request goes to the auto-fix, and the brief and prompt now cover
    infrastructure faults. Documented the existing person fallbacks and added
    verification for each.
  - Q4: Mark blocked always records `help_skipped`, with the fault only in the
    text.
  - Added the shared `HelpContinueMode` / `help_continue_mode` to `case_faults.py`,
    so the services, the brief and the templates share one rule.
  - Dropped the live fault field on the run socket: `RunHelpState` is an enum, so
    the run banner renders the fault server-side.
  - Split the implementation into Phase 0 (shared), five parallel parts, and
    integration.
- 2026-10-26: review pass 1 fixes.
  - `HelpContinueMode` moved to `projects/models.py` (TextChoices) and stored on
    `CaseHelpRequest.continue_mode` when the case pauses
    (`help_continue_mode(fault, checkpoint)`), instead of re-parsing the
    checkpoint on every panel, brief and socket payload (reviewer: JSON load on
    the socket path). Agent-asked requests and older rows default to
    `current_screen`.
  - A fault that interrupted the restore sub-task (checkpoint phase `resume`)
    now continues in `restore` mode (verifier).
  - A fault during the verdict names the last step instead of `len(plan) + 1`;
    the cursor is untouched, so Continue still goes on to the verdict.
  - From-scratch hints no longer promise the note reaches the agent; the redesign
    hint follows `continue_mode` (restore runs cleanup and a restore).
  - `fault_label` / `continue_mode` moved into `machine_setup/payloads.py`
    `HelpThreadPayload`; the brief's `continue_mode` is typed `HelpContinueMode`.
  - Stale "blocks the case" comments outside the diff updated; root `CLAUDE.md`
    `runner_outdated` line updated.
  - Not changed: `HelpContinueMode` placement nit (now in models with the other
    help enums). The "live-continue path" fallback was not needed: every
    Continue goes through `_rebuild_state`.
- 2026-10-26: review pass 2 fixes.
  - A fault before a pending restore finished (for example the visual-engine wait
    on a Continue in `restore` mode) now stays `restore`:
    `execute_test_run_test_case` records the restore-attempt count when a restore
    is due, and `help_continue_mode(..., restore_pending=)` compares it at the
    fault. This replaces the pass-1 `phase == "resume"` rule, which missed faults
    before the restore started (verifier).
  - Root `CLAUDE.md` and `agents/CLAUDE.md` Need help lines describe the three
    continue modes; the case page's two blocked hints get `x-cloak` (reviewer).
  - Rejected (accepted residual): after a fault during the verdict, the help line
    and verdict note say step N "was interrupted" and "retried" although only the
    verdict was. Telling this apart needs a new checkpoint key; the case is rare
    (a fault in the final verdict call) and the verdict itself is still correct.
