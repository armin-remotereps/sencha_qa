# feat: Human in the loop (Need help case status, sleeping run, push notifications, night auto-fix)

Status: Done
Created: 2026-10-24

## Request

Original request: "Sometime the reason that a step is failed on a test, is not a real fail, it may be a timeout on installing something, or clicking an element or something that a human can fix in a blink. When this situation happens, test should go to a new state named need help. And the whole run should go to sleep until a human unblocks that test and resume the run. We need push notifications as well, so when this happens, a user notified immediately. Right now 90 percent of our tests are failing because of that, so we should tell orchestrator to not to hesitate to mark a test as need help."

Addition, after the first draft: each run gets a **help mode**, chosen at Start and kept on the run.

- **Attended**: Need help pauses the run and notifies people immediately.
- **Night**: on Need help, a new Machine setup chat thread is opened automatically, and the problem is handed to the setup agent with the case, the step, the help reason and the failed attempt's actions digest. If the agent fixes it, the case continues automatically from its checkpoint at the failing step. If it cannot, the Attended flow takes over.
- The setup agent gets no guardrails beyond what Machine setup already has: no new time budget, no iteration cap, and no extra restrictions.

Restated: when the orchestrator decides that a failed step is an environmental or transient obstacle rather than a product defect (an install that timed out, an element it cannot click, a stray dialog, a UAC prompt, a slow machine), the case gets a new status, **Need help**. The case is not finalized, and the machine is left exactly as it is (no teardown, no cleanup). The whole run sleeps: no other case runs.

- **Attended run**: every project member with notifications enabled gets a push immediately. A person fixes the machine, then clicks **Continue**, optionally with a note for the agent. The case resumes from its checkpoint, re-runs the failing step, and the run carries on with its remaining cases.
- **Night run**: the setup agent gets the problem first, in its own visible chat thread, and reports fixed or not fixed. Fixed: the case continues on its own, with the agent's summary as the note. Not fixed, or stopped: the Attended flow takes over (push, human).

The orchestrator prompt changes so it picks Need help readily for environmental problems and still fails real product defects.

## Context

**Case outcomes**
- `projects/models.py` defines `TestRunTestCaseStatus` (created, in_progress, success, failed, cancelled, blocked) and `BlockedReason`.
- `TestRunStatus` is waiting, started, done or cancelled.
- Failed comes only from the agent's verdict (`_finalize_pivot`).
- Infrastructure faults become Blocked (`_settle_pivot_after_error` → `case_blocking.classify_case_exception`).

**Execution** (`projects/services.py` `execute_test_run_test_case`)
- A run is one Celery `chain` of `execute_test_run_case.si(pivot_id, generation)` on the `execution` queue, in `RUN_CASE_ORDERING` (`queue_test_run_cases`).
- Each task stands down when the run is CANCELLED or its generation is stale.
- Before the orchestrator, each task runs `_safe_cleanup(wait_for_runner=True)`. That is the runner's `cleanup_environment`: it closes the Playwright browser, terminates interactive sessions, kills tracked processes and empties the cleanup folder (`controller_client/cleanup.py`).
- In `finally`, each task runs `_safe_teardown` (Failed, or Blocked per `case_teardown_policy.needs_teardown`), then `_safe_cleanup(wait_for_runner=False)`, then `_update_test_run_status_if_needed`.

**Resume / Re-run in place** (`projects/run_resume_services.py`, `run_rerun_services.py`, shared `run_reopen_services.py`)
- Both lock the project, then the run.
- Gates, in `list_reopen_blockers`: no other STARTED run, no live TestRail push, machine READY, no live setup turn (`machine_setup.turn_state.live_turn_exists`).
- `reopen_run` sets STARTED, bumps `execution_generation` and queues the pivots after commit.
- A resumed case loads `execution_checkpoint`, skips planning and runs a restore sub-task (`_restore_resumed_state`) before the cursor step.
- "Re-run failed / blocked in a new run" (`rerun_cases`) creates a WAITING run that the user then Starts. `start_test_run(test_run)` is called from `test_run_start`, and a second time in `projects/views.py` (~1272).

**Orchestrator** (`agents/services/orchestrator.py`)
- `_execute_sub_tasks` calls `_evaluate_failure`, which decides continue, recover or stop (`orchestrator_prompts.build_evaluate_prompt`).
- A failed or capped recovery means stop, and stop means Failed.
- Checkpoint (`agents/checkpoint.py`, `CHECKPOINT_VERSION = 1`): new keys must be optional. Unknown phases fall back to "step", and unknown decision actions are dropped.

**Machine setup** (`docs/changed/2026-10-23-machine-setup-*.md`, `machine_setup/`)
- `SetupConversation`: one active per project, enforced by the `setup_one_active_conversation` constraint on `archived_at IS NULL`. It holds `llm_transcript`.
- `SetupTurn`: one live (queued or running) per project, enforced by `setup_one_live_turn_per_project`. Fields include `requested_by`, `stop_requested`, `status`, `ended_reason` and `iterations`.
- `SetupMessage`: the display rows, with nested sub-task rows.
- `send_message.send_setup_message`:
  - locks the project;
  - refuses on a live turn, a STARTED run or a machine that is not READY;
  - creates the turn plus the USER message;
  - on commit, broadcasts and enqueues `machine_setup.tasks.run_setup_turn(turn_id)` on the **`execution`** queue.
- `run_turn.run_setup_turn`:
  - claims the turn (QUEUED → RUNNING) and replays the transcript;
  - runs `run_tool_loop` with `main_tool_definitions()` (project-data tools + `run_setup_task`) and `build_setup_dispatcher(author=turn.requested_by, …)`;
  - `_finish` writes the outcome (`turn_outcome.classify_turn_end`) with a conditional UPDATE and broadcasts.
- Existing limits:
  - `MACHINE_SETUP_MAX_ITERATIONS` (200 main rounds);
  - `MACHINE_SETUP_TURN_TIMEOUT_SECONDS` (3600), plus a Celery soft limit of +300 s and a hard limit of +60 s;
  - per sub-task, `MACHINE_SETUP_SUBTASK_MAX_ITERATIONS` (100) and a timeout of 150–1800 s, clamped to the remaining turn time.
  - A turn that ends without an answer gets "Reply 'continue' to carry on".
- Turn outcome: there is no structured "fixed" result today. The main agent's last reply is free text; only sub-tasks have a parsed `RESULT` (DONE / FAILED / NEEDS_USER / UNCLEAR).
- Existing safety rules in `setup_prompt.py`: destructive work needs the user's confirmation, and a sub-task returning NEEDS_USER makes the main agent ask the user and end its turn.
- Active-conversation queries filter on `archived_at__isnull=True` in `send_message.py`, `chat_page.py` and `reset_conversation.py`.
- `MachineSetupConsumer` (`ws/projects/<id>/machine-setup/`, per-project group) sends `current_state` for the active conversation. Message and turn payloads carry `conversation_id`.

**"Active run" checks** use `status == STARTED` across `projects/services.py`, `run_reopen_services`, `controller_reconnect_services`, `dashboard/services.py` and `machine_setup/services/send_message.py` / `chat_page.py`.

**Notifications**: none exist. There is no service worker, manifest, push library, email backend or per-user channel (verified). Production runs HTTPS behind nginx. Celery has two queues: `upload` (`-Q upload,celery`) and `execution`.

**Conventions**: `projects` has a legacy `services.py` module, so new projects use cases are flat `projects/<use_case>_services.py` modules (as `run_resume_services.py` is). New `machine_setup` code goes in `machine_setup/services/`. The new `notifications` app gets a proper `services/` package.

## Goals

- New case status **Need help** and new run status **Needs help** (`paused`; the run is asleep).
- Per-run **help mode** (`attended` / `night`), chosen at Start and kept on the run, with defaults for every way a run is (re)started.
- The orchestrator can decide `need_help`. The case stops without a verdict, the cursor stays on the failing step, and the machine state is preserved: no teardown or cleanup at pause, no cleanup at continue.
- While a run is paused:
  - nothing else in the run executes;
  - no other run starts in the project;
  - TestRail push and check ignore the Need help case.
- Human actions on a Need help case, available both while an auto-fix is running and after it: **Continue** (optional note), **Mark failed**, **Skip** (Blocked, resumable), **Stop auto-fix**, and **Abort run**.
- Continue resumes from the checkpoint at the failing step without the restore sub-task, and re-runs that step with the note and the failed attempt's actions in its state.
- **Night auto-fix**: a new, visible Machine setup thread linked from the case. The existing setup machinery with its existing limits runs it. A structured "fixed / not fixed" report decides whether the case continues automatically or falls back to Attended.
- Web Push to every project member with notifications enabled (Attended pause, or Night fallback), plus in-app signals.
- Prompt wording that makes Need help the default for environmental or transient failures.

## Non-goals

- A sub-agent tool that pauses mid-step (`request_human_help`). Decided in Q7: the orchestrator decides after the step fails.
- Turning infrastructure Blocked cases into Need help (Q8).
- Automatic expiry of, or reminders for, a sleeping run (Q3).
- New limits or guardrails on the auto-fix setup agent beyond Machine setup's existing ones (user decision).
- Email, Slack or SMS; a notification inbox; remote control of the machine from the browser.
- Any controller protocol change.

## Design

### Data model

`projects/models.py`, migration `projects/0027_need_help` (additive; choices do not change columns):

- `TestRunTestCaseStatus.NEED_HELP = "need_help", "Need help"`.
- `TestRunStatus.PAUSED = "paused", "Needs help"`.
- `BlockedReason.HELP_SKIPPED = "help_skipped", "Skipped while waiting for help"`.
- `ACTIVE_RUN_STATUSES = (STARTED, PAUSED)`.
- `RunHelpMode(TextChoices)`: `ATTENDED = "attended", "Attended"`, `NIGHT = "night", "Night (auto-fix first)"`.
- `TestRun.help_mode`: CharField(16), default `attended`, not null.
  - The migration default fills existing rows, which is cheap on Postgres 11+: a constant default needs no table rewrite.
- New model **`CaseHelpRequest`**, one row per Need help event:
  - `test_run_test_case` FK (CASCADE, `related_name="help_requests"`);
  - `step_num`;
  - `reason` and `ask`: model-written, each capped at about 1000 chars;
  - `requested_at`;
  - `auto_fix_state`: blank = none, then `running` / `fixed` / `not_fixed`;
  - `resolution`: blank = open, then `continued` / `auto_fixed` / `marked_failed` / `skipped` / `cancelled`;
  - `resolved_by` FK user (SET_NULL; null for `auto_fixed`), `resolved_at`;
  - `note`: the human's note, or for `auto_fixed` the setup agent's summary; capped at 2000 chars.
  - `UniqueConstraint(fields=["test_run_test_case"], condition=Q(resolution=""))`, so at most one open request per case.
  - Index `(test_run_test_case, step_num, -requested_at)`.
- `TestRunTestCase` itself does not change. While Need help: `status=need_help`, `result` = the ask text, `finished_at` null, checkpoint kept.

`machine_setup/models.py`, migration `machine_setup/0003_case_auto_fix` (machine_setup already depends on projects, so the FK direction adds no cross-app cycle):

- `SetupConversationKind(TextChoices)`: `CHAT = "chat"`, `AUTO_FIX = "auto_fix", "Auto-fix"`.
- New fields on `SetupConversation`:
  - `kind` (default `chat`);
  - `help_request` FK → `projects.CaseHelpRequest`, null, SET_NULL, `related_name="setup_conversations"`;
  - `fix_result` (blank / `fixed` / `not_fixed`);
  - `fix_summary` (text, capped at 2000).
- `setup_one_active_conversation` becomes a constraint on `(project)` with condition `archived_at IS NULL AND kind = 'chat'`. Auto-fix threads therefore never collide with the main chat or with each other. The migration drops and re-creates the constraint; existing rows are all `chat`.
- `SetupTurn.auto_fix` (bool, default False) marks the one turn the server starts for an auto-fix. Later turns a person starts in that thread are ordinary turns.
- `setup_one_live_turn_per_project` is unchanged: one agent drives the machine at a time.

Checkpoint (`agents/checkpoint.py`, `agents/types.py`). The version stays 1 and every new key is optional:

- phase `"awaiting_help"`;
- decision action `"need_help"`;
- optional key `help: {step_num, request, note, source: "human"|"auto_fix", continue_pending}`.

A rolled-back server ignores the key, falls back to phase "step", drops the unknown decision, and still resumes at the cursor.

New app **`notifications`**, migration `0001`:

- `PushSubscription`: user FK, `endpoint` (URLField 1000, unique), `p256dh`, `auth`, `user_agent`, `created_at`, `last_success_at`.
- The endpoint is never rendered, and the admin is read-only.

### Services

**agents** (ORM-free):

- `agents/types.py`:
  - `OrchestratorDecision` gains action `"need_help"`, `cause` (`environment` / `agent` / `product` / `unclear`) and `help_ask`;
  - `OrchestratorResult.status` gains `"need_help"` and a `help: HelpRequestInfo | None` (step_num, reason, ask);
  - `AgentStopReason.NEEDS_HUMAN_HELP`, and `AgentResult.help`.
- `orchestrator_prompts.py`: the evaluate prompt gets the new decision and policy (wording below). The verdict prompt gets a `was_helped` note, so the verdict says whether a person or the setup agent helped at step N.
- `orchestrator.py`:
  - **need_help**: record the decision, keep `next_step` on the failed step, set phase `awaiting_help`, write the checkpoint, and return `need_help` **without** the verdict call.
  - **Escalation**: a recover decision that cannot go ahead (recovery failed, cap reached, or no task) whose `cause != product` becomes need_help instead of stop.
  - **Continue after help** (`resume.help.continue_pending`):
    - skip `_restore_resumed_state`;
    - add the state line "Step N: <a person | the machine setup agent> fixed the machine after the agent asked: <ask>. Note: <note>";
    - restore `retry_notes[N]` (the failed attempt's digest);
    - clear `continue_pending` on the first checkpoint.
  - `_rebuild_state` builds the same line from the checkpoint's `help` key.
  - The orchestrator is unaware of the help mode. Attended and Night differ only after the pause, in `projects` / `machine_setup`. The global `HUMAN_HELP_ENABLED` switch from the first draft is **dropped**: the per-run mode replaces Q5.
- `sub_agent_prompts.py` / `prompt_parts.py`: on environmental obstacles, the executor fails fast with `RESULT: FAIL` and a SUMMARY starting `ENVIRONMENT:` that says what is wrong and what a person would need to do.

**Prompt wording** (load-bearing; polish allowed, policy kept):

```
- "need_help": the failure is caused by the test machine or environment, not by the
  application under test, and a person at the machine can likely fix it quickly
  (an install/download/build that timed out or stalled, a permission/UAC/admin prompt,
  an unexpected dialog or popup, an element that is on screen but does not react to
  clicks, a missing tool or file, a network hiccup, a slow or frozen machine). The run
  pauses, someone fixes the machine, and this sub-task is retried from the current state.
- "stop": ONLY when the application under test itself behaves contrary to the expected
  result (a real product defect).
Prefer "need_help" over "stop" whenever you are not sure the application is at fault —
a wrong Failed costs a bug ticket; a Need help costs a person one minute.
Use "recover" only when the agent itself can clearly fix it (it clicked the wrong thing,
or a long operation is still legitimately running and needs more time) — never more than
once for the same environmental problem.
Output adds: "cause": "environment"|"agent"|"product"|"unclear",
"help_ask": "<one or two imperative sentences: what to fix on the machine>"
(help_ask only when decision is need_help or recover).
```

**projects** (every write respects `execution_generation`):

- `projects/services.py` (execution path):
  - `execute_test_run_test_case`:
    - stands down when the run is PAUSED;
    - skips the pre-case `_safe_cleanup` when the checkpoint has `help.continue_pending`;
    - on `NEEDS_HUMAN_HELP`, calls `_pause_run_for_help(pivot, help, generation)`;
    - in `finally`, skips teardown and cleanup for a NEED_HELP pivot;
    - `_update_test_run_status_if_needed` returns early for PAUSED.
  - `_pause_run_for_help`, in one transaction:
    - lock the run; return if the generation is stale; cancel the case if the run is no longer STARTED;
    - pivot → NEED_HELP; create the `CaseHelpRequest`; run → PAUSED; append the log line;
    - decide `auto_fix` = `run.help_mode == NIGHT and auto_fix_eligible(pivot, step)` (`projects/case_help_policy.py`, pure plus one query; the Q10 rule).
    - On commit, broadcast. Then:
      - if `auto_fix` is true, set `auto_fix_state=running` in the same transaction and enqueue the task named `machine_setup.task_names.START_CASE_AUTO_FIX`. It is sent with `celery_app.send_task` by name, because importing `machine_setup.tasks` from `projects.services` would be an import cycle (`machine_setup.services` imports `projects.services`); `task_names.py` is a leaf constant module.
      - otherwise call `case_help_notifications.notify_case_needs_help(help_request_id)`.
  - `abort_test_run` accepts STARTED or PAUSED. It does four things:
    - sets NEED_HELP pivots to Cancelled (resumable);
    - resolves the open request as `cancelled`;
    - asks a running auto-fix turn to stop through the leaf `machine_setup/auto_fix_stop.py` (a filtered UPDATE that sets `stop_requested` on the live `auto_fix` turn of that request; it imports only `machine_setup.models`);
    - for a paused run, does not revoke anything.
  - `start_test_run(test_run, *, help_mode)` saves the mode under the same lock.
  - `rerun_cases` copies `help_mode` from the source run into the new WAITING run, which pre-selects it at Start.
  - `ACTIVE_RUN_STATUSES` is used in `_has_active_test_run`, `_no_active_run_check`, the overview, `can_edit_test_case_in_run` and the narrative.
  - `get_test_run_summary` gains `need_help`, and the event payloads carry the new values.
  - `abort_active_test_run_on_disconnect` stays STARTED-only.
- `projects/run_reopen_services.py`:
  - `_has_other_started_run` uses `ACTIVE_RUN_STATUSES`;
  - `reopen_run(test_run, pivots, *, help_mode=None)` optionally changes the mode;
  - new `queue_pending_cases(test_run)` queues every CREATED pivot in `RUN_CASE_ORDERING`. The helped case is CREATED and earlier than the rest, so it runs first.
- `projects/run_resume_services.py` and `run_rerun_services.py`: the run keeps its mode by default. The Resume and Re-run confirm dialogs get an optional `help_mode` select pre-set to the run's current mode, passed through to `reopen_run`.
- **New `projects/case_help_policy.py`**: `auto_fix_eligible(pivot_id, step_num) -> bool`. It is False when an earlier request on this pivot for the same step has `auto_fix_state=fixed` since the case last started from scratch (Start, or a re-run in place `TestRunCaseResume(kind=rerun)`). This is Q10's assumption: a repeat of the same step goes straight to a human.
- **New `projects/case_help_services.py`** (the human and auto-fix resolution use cases; imports `run_reopen_services`, `services`, and the leaf `machine_setup.auto_fix_stop` / `turn_state`, never `machine_setup.services`):
  - Read side: `get_help_panel(pivot | run)` returns:
    - the open request and its auto-fix state;
    - the link to the auto-fix thread, via `select_related` / `Prefetch` on `setup_conversations`;
    - which actions are allowed, and the blockers.

    Also `list_case_help_requests(pivot)` and `get_open_help_request(test_run)`.
  - Every action locks project → run, requires the run to be PAUSED, and resolves the open request.
    - `continue_after_help(*, pivot, user, note)`: writes `help` (`source=human`, `continue_pending=True`) into the checkpoint and sets the pivot to CREATED.
    - `mark_failed_after_help(*, pivot, user, note)`: pivot → FAILED with a server-built `result` and `finished_at`.
    - `skip_after_help(*, pivot, user, note)`: pivot → BLOCKED `help_skipped`.
    - Then, if no auto-fix turn is live, `queue_pending_cases`, subject to the gates. With nothing left to queue, the run → DONE.
    - If an auto-fix turn is live, request its stop (`auto_fix_stop`). The run is queued when that turn ends (`finish_case_auto_fix` below), and the page says "continues as soon as the auto-fix agent stops".
  - `continue_paused_run(*, test_run, user)`: for a PAUSED run whose request is already resolved but whose queueing failed its gates (runner offline when the auto-fix ended, for example). It is the run-page "Continue run" button.
  - `finish_case_auto_fix(*, help_request_id, fixed, summary)`, called by machine_setup when the auto-fix turn ends. It locks project → run and is a no-op unless the run is PAUSED:
    - **request still open, fixed**: auto_fix_state `fixed`, resolution `auto_fixed`, note = summary, checkpoint `help` (`source=auto_fix`, `continue_pending=True`), pivot CREATED, `queue_pending_cases`. If the gates fail (runner gone, for example), re-open the request as human-pending (auto_fix_state `fixed`, resolution blank, pivot NEED_HELP) and notify.
    - **request still open, not fixed or stopped**: auto_fix_state `not_fixed`; the request stays open; `notify_case_needs_help(…, after_auto_fix=True)`. This is the Attended fallback.
    - **request already resolved by a person during the auto-fix**: `queue_pending_cases` (gates as above).
  - `RunHelpError(message)` in `projects/exceptions.py`.
- **New `projects/case_help_notifications.py`**:
  - `notify_case_needs_help(help_request_id, *, after_auto_fix=False)` builds the `PushMessage`;
  - title: "Needs help · <project>", or "Auto-fix could not fix it · <project>";
  - body: case title plus the ask, truncated to about 180 chars;
  - url: the case page; tag: `need-help-<pivot_id>`;
  - recipients: all project members (Q1), one query. It enqueues `notifications.tasks.send_push` and never raises.

**machine_setup** (reuses the chat, turn, sub-task and recorder machinery; no parallel agent):

- **New `machine_setup/task_names.py`** and **`machine_setup/auto_fix_stop.py`**: leaf modules, described above.
- **New `machine_setup/services/case_auto_fix.py`**:
  - `start_case_auto_fix(help_request_id)` runs in the Celery task, under the project lock.
    - If the request is not open, or the run is not PAUSED, it does nothing.
    - If a turn is live (someone is chatting) or the machine is not READY, it calls `case_help_services.finish_case_auto_fix(fixed=False, summary="Auto-fix could not start: …")`, i.e. the Attended fallback.
    - Otherwise it creates a `SetupConversation(kind=auto_fix, help_request=…, created_by=None)`, a `SetupTurn(auto_fix=True, requested_by=None)` and a USER `SetupMessage` (author None) whose text is the brief below. On commit it broadcasts and enqueues the existing `run_setup_turn(turn_id)` on `execution`. This mirrors `send_message._create_turn` / `_broadcast_and_enqueue`; extract the shared part rather than copy it.
  - `settle_case_auto_fix(turn)`: called by `run_turn._finish` **after** the turn's status UPDATE (so `live_turn_exists` is already false), only when `turn.auto_fix`. It reads `conversation.fix_result` / `fix_summary` (`not_fixed` when the agent never reported, was stopped, hit a limit, or failed) and calls `case_help_services.finish_case_auto_fix`. It never raises.
- **New `machine_setup/auto_fix_brief.py`** (pure): `build_auto_fix_brief(...)` makes the first user message. It contains:
  - the case title, preconditions, steps and expected result;
  - the plan steps already done;
  - step N's description and expected result;
  - the orchestrator's reason and ask;
  - the failed attempt's `actions_digest` (≤ 6000 chars, from the checkpoint);
  - context sentences, which describe and do not restrict: "Nobody is watching this run right now. After you report the machine fixed, the test resumes on its own and re-runs step N from the current screen. When you are done, call report_case_fix."

  The data is loaded by `case_auto_fix.py` (`select_related` pivot → case, run, project; one query).
- `machine_setup/tasks.py`: new `start_case_auto_fix(help_request_id)` with the name from `task_names`, routed to **`execution`** like setup turns, with a short soft limit (it only creates rows).
- `machine_setup/services/setup_tools.py`:
  - `main_tool_definitions(auto_fix=False)` adds a **`report_case_fix(fixed: boolean, summary: string)`** tool for auto-fix turns only;
  - the dispatcher writes `fix_result` / `fix_summary` on the conversation (the last call wins) and returns a short acknowledgement. This is the structured "fixed / not fixed" channel. It reports; it restricts nothing.
- `machine_setup/services/setup_prompt.py`: one paragraph, used only when `auto_fix`, explaining the report tool and that the brief comes from a paused test run. All existing rules stay as they are. Note: the existing "destructive work needs the user's confirmation" and NEEDS_USER rules still apply. In a night turn the agent may then end its turn asking a question, which counts as not fixed and falls back to a human. That is intended: no rules are added or removed.
- `machine_setup/services/run_turn.py`:
  - passes `auto_fix=turn.auto_fix` into the tool definitions, dispatcher and prompt;
  - `_finish` calls `settle_case_auto_fix` last;
  - `build_setup_dispatcher(author=None)` already accepts None.
  - Limits are exactly the existing ones: `MACHINE_SETUP_MAX_ITERATIONS`, `MACHINE_SETUP_TURN_TIMEOUT_SECONDS`, the sub-task limits and the Celery limits. **No new budget or cap** (user decision). If an hour proves short, raise the existing `MACHINE_SETUP_TURN_TIMEOUT_SECONDS`.
- `send_message.py`, `chat_page.py`, `reset_conversation.py`:
  - "active conversation" queries add `kind=chat`;
  - `send_setup_message` accepts an optional `conversation` (a non-archived thread of this project, chat or auto_fix), so a person can reply in an auto-fix thread after its turn ends;
  - `_refusal` is unchanged: a STARTED run still blocks; a PAUSED run does not (Q6).
  - Reset archives only the main chat.
- `chat_page.py`:
  - `get_setup_page(project, conversation=None)`;
  - `list_auto_fix_threads(project, limit=20)`: one query with `select_related("help_request__test_run_test_case__test_case")` and the latest turn status, via `Subquery`.

**notifications** (new app, `notifications/services/`):

- `subscriptions.py`: save, delete and list subscriptions.
- `send_push.py`: `send_push_to_users`. Subscriptions are loaded in one query; `pywebpush.webpush` is called with TTL, timeout and `Urgency: high`; a 404/410 deletes the row; `last_success_at` is updated in bulk.
- `push_message.py`: the `PushMessage` dataclass.
- `endpoint_policy.py`: an SSRF allowlist (https, host suffix in `WEBPUSH_ALLOWED_ENDPOINT_HOSTS`).
- `exceptions.py`.
- `tasks.py`: `send_push` on the `upload` queue, `max_retries=0`.

### Pages and forms

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `projects:test_run_start` (existing) | POST | `@project_membership_required` | new `TestRunStartForm` (`help_mode`, radio, initial = `run.help_mode`) | redirect to run page; the start panel shows the two modes with one-line explanations | invalid mode → message; existing ValueErrors |
| `projects:test_run_resume`, `test_run_case_resume`, `test_run_cases_rerun` (existing) | POST | same | existing forms + optional `help_mode` (initial = run's) | unchanged redirects | unchanged |
| `projects:test_run_case_help_continue` `/projects/<id>/test-runs/<run_id>/cases/<pivot_id>/help/continue/` | POST | same | `CaseHelpResolutionForm` (`note`, optional, max 2000) | redirect to case page | `RunHelpError` → `messages.error`; 404 |
| `projects:test_run_case_help_fail` `.../help/fail/` | POST | same | same | same | same |
| `projects:test_run_case_help_skip` `.../help/skip/` | POST | same | same | same | same |
| `projects:test_run_case_help_stop_auto_fix` `.../help/stop-auto-fix/` | POST | same | — | redirect to case page (stop requests the auto-fix turn's stop; the fallback then notifies) | no live auto-fix → message |
| `projects:test_run_continue` `/projects/<id>/test-runs/<run_id>/continue/` | POST | same | — | redirect to run page | `RunHelpError` |
| `projects:test_run_abort` (existing) | POST | same | — | — | now also for a paused run |
| `machine_setup:thread` `/projects/<id>/machine-setup/threads/<conversation_id>/` | GET | same | — | `machine_setup/chat.html` with `conversation` (that thread), `thread_kind`, `help_case_url`, `threads` (list) | 404 if the thread is not this project's |
| `machine_setup:send` (existing) | POST | same | `SetupMessageForm` + optional `conversation_id` | redirect to the chat or thread | existing errors; a foreign or archived thread → 404 |
| `notifications:settings` `/notifications/` | GET | `@login_required` | — | `push_configured`, `vapid_public_key`, `devices` (never endpoints) | — |
| `notifications:subscribe` `/notifications/subscriptions/` | POST (fetch, CSRF) | `@login_required` | `PushSubscriptionForm` | 204 | 400 on invalid input or a disallowed host |
| `notifications:unsubscribe` `/notifications/subscriptions/remove/` | POST | `@login_required` | `PushUnsubscribeForm` | 204, or redirect | own rows only |
| `notifications:test` `/notifications/test/` | POST | `@login_required`, throttled | — | redirect | push disabled → message |
| `notifications:service_worker` `/sw.js` | GET | public | — | JS, `no-cache`, root scope | — |
| `notifications:manifest` `/manifest.webmanifest` | GET | public | — | JSON manifest (standalone, icons 192/512) | — |

Templates:

- **Case page**: a "This case needs help" card showing the step, the agent's reason and ask (escaped), and how long it has been waiting.
  - While the auto-fix runs: "The machine setup agent is trying to fix this (thread link)", with Stop auto-fix, Continue, Mark failed, Skip and Abort run. The three resolution actions say "takes effect when the auto-fix agent stops".
  - After a failed auto-fix: "Auto-fix could not fix it: <summary>", plus the thread link and the actions.
  - A help request history list.
- **Run page**:
  - a banner "Run paused: <case> needs help" or "… auto-fixing", with links;
  - a "Continue run" button when the request is resolved but queueing waits for the gates;
  - the help mode shown in the header;
  - the start panel with the mode radio;
  - a mode select in the Resume and Re-run dialogs;
  - status maps for `need_help` / `paused`;
  - a Need help stat;
  - a reload on run status change into or out of `paused`, and on auto-fix state change. The `TestRunStatusEvent` gains `help_state` (`none` / `auto_fixing` / `waiting_for_person`), so no new event type is needed.
- **Machine setup page**:
  - a "Threads" list: the main chat, then auto-fix threads labelled "Auto-fix · <case> · step N · <fixed | not fixed | running>", linking to `machine_setup:thread`;
  - in a thread view, a header line linking back to the case;
  - the existing composer, enabled under the existing rules.
  - `MachineSetupConsumer` gets a second route, `ws/projects/<id>/machine-setup/threads/<conversation_id>/`, with the same group. `current_state` loads that conversation, and the client ignores rows whose `conversation_id` is not the one shown (payloads already carry it). The route keeps `AllowedHostsOriginValidator`.
- Partials and CSS:
  - badges `need_help` / `paused`, with an "Auto-fixing" variant;
  - `.badge-need-help`, `.alert-need-help`, `.chat-thread-list`;
  - dashboard attention `need_help_run`;
  - overview alert;
  - run list badge.
- Notifications page, `base.html` manifest link, service worker registration in `punk-hazard.js` (`pushDevice` component), sidebar link, `sw.js` (`push` → `showNotification`; `notificationclick` → same-origin path only).

### Background work and WebSockets

**Pause.** The case worker writes NEED_HELP + PAUSED and returns. The chain's remaining tasks see PAUSED and exit; the remaining cases stay Created. No worker waits for a person.

**Attended.** A push goes out on commit (`upload` queue). A human action queues the pending cases through `queue_pending_cases`, which bumps the generation and starts a new chain.

**Night.** Sequence:
1. `start_case_auto_fix` (on `execution`) creates the thread and turn, then enqueues `run_setup_turn` (on `execution`).
2. The setup turn runs with its existing limits and calls `report_case_fix`.
3. `_finish` calls `settle_case_auto_fix`, which calls `finish_case_auto_fix`.
4. Fixed: the pending cases are queued, the helped case first. Not fixed: push and wait for a human.

During the auto-fix the run stays PAUSED, the case stays NEED_HELP, and `auto_fix_state=running`. That is the "auto-fixing" sub-state, distinct in the UI without a new status value. Because the setup turn is the project's one live turn, Start, Resume and Re-run are already refused by the existing `live_turn_exists` gate.

**Human actions during an auto-fix** resolve the request at once, and ask the turn to stop at its next tool boundary (existing stop semantics). The run is queued when the turn's `_finish` runs. Abort cancels at once, and the late `finish_case_auto_fix` is a no-op on the cancelled run.

**Stale or killed setup worker.** The existing stale-turn handling (`live_turn.resolve_live_turn` closes it as `worker_lost`) runs whenever someone next opens the chat or sends a message. It must also call `settle_case_auto_fix` for an `auto_fix` turn, so a killed worker falls back to Attended. The case page's help panel triggers that resolve too.

**WebSocket.** No new event types:
- the run and case events carry the new status values and `help_state`;
- `summary` gains `need_help`;
- step progress gains `AWAITING_HELP`;
- setup events are unchanged apart from the extra thread route.

**Controller.** No protocol change and no version or capability change.

### Settings / infrastructure

All read in `auto_tester/settings.py` via decouple and listed in `example.env`:

- `WEBPUSH_VAPID_PUBLIC_KEY`, `WEBPUSH_VAPID_PRIVATE_KEY` (secret), `WEBPUSH_VAPID_SUBJECT`. Empty means push is disabled.
- `WEBPUSH_TTL_SECONDS` (86400), `WEBPUSH_REQUEST_TIMEOUT` (10).
- `WEBPUSH_ALLOWED_ENDPOINT_HOSTS` (Csv; default FCM, Mozilla, Apple, WNS suffixes).
- **No** `HUMAN_HELP_ENABLED` (replaced by the per-run mode) and **no** new machine setup limits.
- `INSTALLED_APPS += "notifications"`.
- `CELERY_TASK_ROUTES`: `notifications.tasks.send_push` → `notify` (own worker, see Changelog); `machine_setup.tasks.start_case_auto_fix` → `execution`.
- `auto_tester/urls.py` includes `notifications.urls` (root for `/sw.js` and the manifest).
- `requirements.txt`: `pywebpush~=2.5.0` (latest on PyPI, 2026-08-30; brings py-vapid, http-ece, requests, aiohttp; needs cryptography>=47, which is satisfied), plus a local stub `stubs/pywebpush.pyi`.
- No nginx or compose change.

### Data flow

1. A step fails. The evaluate step returns `need_help`, or a non-product recovery fails. The checkpoint is saved with cursor = N and phase `awaiting_help`, and the orchestrator returns `NEEDS_HUMAN_HELP`.
2. `_pause_run_for_help`: pivot NEED_HELP, request opened, run PAUSED, all in one generation-checked transaction. No teardown or cleanup.
3. **Attended** (or Night, not eligible): on commit, broadcast, then push to project members.
4. **Night**, eligible: `auto_fix_state=running`, then on commit `start_case_auto_fix` creates thread T and turn X, and `run_setup_turn(X)` runs. The case page and run banner link to T, and the thread shows live in Machine setup.
5. X ends (any reason), and `settle_case_auto_fix` → `finish_case_auto_fix`:
   - fixed and gates OK: resolution `auto_fixed`, checkpoint `help(source=auto_fix, note=summary, continue_pending)`, pivot CREATED, new chain;
   - not fixed: push "Auto-fix could not fix it", and the request waits for a human.
6. A human (any time): Continue, Mark failed, Skip, Stop auto-fix or Abort. Continue / Mark failed / Skip queue the pending cases immediately, or when the live auto-fix stops.
7. The worker picks up the helped case. It skips the pre-case cleanup, does not restore, adds the help line and the retry note to the state, re-runs step N, and continues to the verdict, which mentions the help. The run then carries on and may pause again later.
8. The same step fails again after an auto-fix reported it fixed: `auto_fix_eligible` is False, so it goes straight to Attended (Q10).

## Alternatives considered

- **Worker blocks waiting for a human.** Rejected: it ties up a worker slot for hours and runs into `soft_time_limit` and the 4 h visibility timeout. Re-queuing via `reopen_run` reuses the proven Resume path.
- **Cutting the Celery chain.** Not needed: PAUSED makes the leftover tasks no-ops.
- **Re-using Blocked + Resume.** Rejected: Blocked tears down and cleans up the machine, and the run would continue instead of sleeping.
- **Sub-agent `request_human_help` tool.** Deferred (Q7).
- **Night auto-fix as its own agent, or as a recovery sub-agent inside the case worker.** Rejected at the user's request. Reusing the setup turn gives its tools, visibility, Stop, transcript and limits for free, and it frees the execution slot of the case.
- **Auto-fix "fixed" taken from the main agent's free-text reply or the last sub-task's RESULT.** Rejected as ambiguous. The `report_case_fix` tool is an explicit, structured report and adds no restriction.
- **Starting the auto-fix through `send_setup_message`.** Not reused directly: it refuses non-chat state, requires a user, and writes into the main chat. The shared turn-creation and enqueue part is extracted and reused instead.
- **Auto-fix in the main chat conversation.** Rejected: the user asked for a new thread, and mixing it into a person's ongoing chat would pollute that transcript. Hence `SetupConversation.kind` and the relaxed uniqueness constraint.
- **A new run or case status for "auto-fixing".** Rejected: `auto_fix_state` on the request gives a distinct UI state without touching every status switch twice.
- **A third-party push service or a per-user WebSocket channel.** Not chosen (as in the first draft): vendor and data concerns. (A dedicated `notify` queue was added after review, for push latency.)
- **A global `HUMAN_HELP_ENABLED` switch.** Replaced by the per-run help mode.

## Risks and mitigations

- **Night auto-fix runs unattended with shell access and no extra guardrails** (the user's explicit choice). Residual risk is the same class as Machine setup today: prompt-only safety, injected case text reaching an agent with shell. Mitigations come from what exists:
  - the existing confirmation and NEEDS_USER rules, which here mean falling back to a human;
  - the thread is visible and auditable;
  - context writes are versioned with a notice;
  - Stop and Abort are available.

  This is stated in the run-start mode description.
- **Auto-fix loops**: bounded by the Q10 rule (one auto-fix per step per from-scratch execution). Different steps can still each get one, so the worst case is about the number of plan steps per case, each within the existing 1 h turn limit.
- **The auto-fix holds the project's machine** for up to an hour per attempt. Human Start/Resume and chat sends are refused meanwhile; Stop auto-fix is the escape.
- **Stale auto-fix turn** (worker killed), **lost settle** or **lost start task**: the periodic `machine_setup.tasks.sweep_stale_auto_fixes` (beat, every 2 min, `notify` queue) closes a stale live auto-fix turn of a paused run through the existing stale resolution (whatever its request's state), settles a `running` request whose turn ended over 2 min ago unsettled from that turn, and settles a request still `running` with no thread 15 min after `auto_fix_started_at` as not fixed; the case falls back to a person with a push, without a page visit.
- **Constraint change on `SetupConversation`**: drop and re-create in one migration. Every active-conversation query must add `kind=chat`, or the main chat could pick up an auto-fix thread. All three call sites are listed.
- **Cross-app calls**: `projects.services` never imports `machine_setup.services` (cycle). It uses leaf modules (`task_names`, `auto_fix_stop`, `turn_state`) and task-by-name. `machine_setup.services.case_auto_fix` → `projects.case_help_services` is the allowed direction.
- **Gates failing at auto-fix end** (runner gone): the request goes back to human-pending and a push is sent, so the run never silently stalls.
- **Accepted risk (user decision, no extra guardrails on the auto-fix agent)**: TestRail case text and screen text reach an unattended setup agent with shell access, guarded only by the prompt's "read as data" rule. Not mitigated further.
- **Over-use of Need help, real defects classified as environment, a run sleeping forever, the cleanup skip consumed once, races (project → run locks, generation checks), SSRF on push endpoints, secrets (VAPID key, endpoints never rendered or logged), XSS (all model, agent and human text autoescaped; `sw.js` same-origin only), human-note injection (members trusted, capped), and iOS limits.** Each is handled as in the first draft.
- **Migrations**:
  - `projects.0027` is additive, with a constant-default column.
  - `machine_setup.0003` changes a constraint and adds columns.
  - `notifications.0001` is new.
  - Rollback: first `UPDATE projects_testruntestcase SET status='blocked', blocked_reason='internal_error' WHERE status='need_help' OR blocked_reason='help_skipped'`, `UPDATE projects_testrun SET status='cancelled' WHERE status='paused'`, and archive (`archived_at=now()`) every `kind='auto_fix'` conversation before reversing `machine_setup.0003`, so the old one-active constraint holds.
- **Checkpoint compatibility**: only optional keys; the version is unchanged.
- **N+1**: the help panel, thread list and recipients each use a bounded number of queries, via `select_related` / `Prefetch` / `Subquery`.

## Implementation steps

Shared code first; then parallel parts that never share a file.

0. **Foundation** (sequential):
   - `projects/models.py` + `0027` (statuses, `RunHelpMode`, `help_mode`, `CaseHelpRequest`, `ACTIVE_RUN_STATUSES`);
   - `projects/exceptions.py` (`RunHelpError`);
   - `agents/types.py` and `agents/checkpoint.py`;
   - `machine_setup/models.py` + `0003`;
   - `machine_setup/task_names.py`, `machine_setup/auto_fix_stop.py`;
   - `notifications` skeleton (apps, models, `0001`, exceptions, `push_message.py`);
   - settings, `example.env`, requirements, `stubs/pywebpush.pyi`.

   Fix these signatures here:
   - `finish_case_auto_fix(*, help_request_id: int, fixed: bool, summary: str) -> None`;
   - `notify_case_needs_help(help_request_id: int, *, after_auto_fix: bool = False) -> None`;
   - `auto_fix_eligible(pivot_id: int, step_num: int) -> bool`;
   - `queue_pending_cases(test_run: TestRun) -> None`;
   - `report_case_fix(fixed, summary)`;
   - context keys `help_panel`, `help_requests`, `open_help_request`, `start_form`;
   - event field `help_state`.
1. **Part A, orchestrator and prompts**: `agents/services/orchestrator.py`, `orchestrator_prompts.py`, `sub_agent_prompts.py`, `prompt_parts.py`, `agents/CLAUDE.md`.
2. **Part B, execution path and run gates**:
   - `projects/services.py` (pause, skips, abort incl. auto-fix stop, `start_test_run(help_mode)`, `rerun_cases` copy, active-run checks, summary, narrative, events);
   - `projects/case_help_policy.py`, `projects/run_reopen_services.py`, `projects/run_resume_services.py`, `projects/run_rerun_services.py`;
   - `projects/case_step_progress.py`, `projects/consumers.py`, `projects/case_teardown_policy.py` (docstring), `dashboard/services.py`.
3. **Part C, human actions and run/case UI**:
   - `projects/case_help_services.py`;
   - `projects/forms.py` (`CaseHelpResolutionForm`, `TestRunStartForm`, `help_mode` on the resume and rerun forms);
   - `projects/views.py`, `projects/urls.py`;
   - `templates/projects/test_run_case_detail.html`, `test_run_detail.html`, `test_runs.html`, `overview.html`;
   - `templates/partials/badge_case_status.html`, `badge_run_status.html`;
   - `templates/dashboard/includes/*`, `static/css/punk-hazard.css`.
4. **Part D, machine setup auto-fix**:
   - `machine_setup/services/case_auto_fix.py`, `machine_setup/auto_fix_brief.py`, `machine_setup/tasks.py`;
   - `machine_setup/services/setup_tools.py`, `setup_prompt.py`, `run_turn.py`, `send_message.py`, `chat_page.py`, `reset_conversation.py`, `live_turn.py`;
   - `machine_setup/views.py`, `urls.py`, `forms.py`, `routing.py`, `consumers.py`;
   - `templates/machine_setup/*`.
   - CSS for the thread list goes in Part C's `punk-hazard.css`: hand Part C the class names.
5. **Part E, notifications**:
   - `projects/case_help_notifications.py`;
   - `notifications/services/*`, `tasks.py`, `forms.py`, `views.py`, `urls.py`, `admin.py`;
   - `templates/notifications/*`;
   - `auto_tester/urls.py`, `templates/base.html`, `templates/partials/sidebar.html` / `nav_links.html`, `static/js/punk-hazard.js`, `static/branding/` icons.
6. **Integration** (orchestrating agent):
   - wire the seams: task routes, thread WebSocket route, imports;
   - check that the import graph has no `projects.services` → `machine_setup.services` edge;
   - `pre-commit run --all-files`, `manage.py check`, `makemigrations --check`.
7. **Docs**: `CLAUDE.md` (Need help, help modes, auto-fix threads, notifications app); `docs/changed/<date>-human-in-the-loop.md`.

## Verification

Manual checks; this project has no automated tests.

1. Pre-commit (black, isort, mypy strict); `manage.py check`; `makemigrations --check --dry-run`; migrate up and down on a scratch database with the rollback SQL.
2. Shell smoke:
   - checkpoint round-trip with `help`, `awaiting_help` and `need_help`;
   - an old checkpoint still loads;
   - `_rebuild_state` with a human help and with an auto-fix help;
   - `auto_fix_eligible` before and after a fixed auto-fix on the same step, on another step, and after a re-run in place.
3. **Attended**, live, with a forced environmental failure (a manually opened modal over the target):
   - the case goes Need help and the run Needs help;
   - the remaining cases stay Not started;
   - windows and browser are untouched;
   - a push arrives, and clicking it opens the case.
4. Continue with a note: no restore and no cleanup; step N re-runs with the note in state; the verdict mentions the help; the next cases run. Repeat with Mark failed and with Skip, then Resume the skipped case.
5. Abort a paused run: the run is Cancelled and the case Cancelled; a later Resume restores and continues it.
6. **Night**, fixable (for example an installer dialog left open):
   - an auto-fix thread appears under Machine setup, linked from the case;
   - the run banner says "auto-fixing" and no push is sent;
   - the agent calls `report_case_fix(fixed=true)`;
   - the case continues by itself, the verdict mentions the setup agent, and the run proceeds.
7. **Night**, not fixable (for example a required credential the agent cannot know):
   - `report_case_fix(fixed=false)`, or no report, or a NEEDS_USER question;
   - push "Auto-fix could not fix it";
   - Continue works as in Attended.
8. **Night** with human actions during the auto-fix:
   - Stop auto-fix leads to the fallback and a push;
   - Continue, Mark failed and Skip during the turn each resolve, the turn stops, and the run queues after it ends;
   - Abort during the turn cancels the run, and the late settle does nothing.
9. **Night**, the same step fails again after a reported fix: it goes straight to a human, with no second thread.
10. **Night** auto-fix start refused (runner offline, or someone's chat turn live): immediate fallback to Attended.
11. Kill the execution worker during an auto-fix, then open the chat page: the turn is closed `worker_lost`, and the fallback push is sent.
12. Help mode defaults:
    - Start shows the radio;
    - Resume and Re-run keep the run's mode, and changing it in the dialog persists;
    - "Re-run failed in a new run" pre-selects the source mode.
13. While paused:
    - Start of other runs is refused;
    - TestRail push and check exclude the case;
    - a runner disconnect does not cancel the run;
    - the main setup chat can be used (Q6), and Continue is disabled while that chat turn is live.
14. A product defect still ends Failed.
15. Notifications page: enable, test, remove; a disallowed endpoint host → 400; a 410 deletes the row; an iOS home-screen push.
16. Run the `reviewer` and `verifier` agents in parallel.

## Follow-ups (2026-10-24)

**F1. The tester sees both the setup agent's and the person's message.**
- `CaseHelpRequest.auto_fix_summary` (text, ≤ 2000, blank; `projects/0028_case_help_auto_fix_summary`, which also moves the summary of existing `auto_fixed` rows out of `note`). `note` is now the person's note only.
- `finish_case_auto_fix` stores the setup agent's summary in `auto_fix_summary` for fixed and not fixed (also when the run is no longer paused, and when a person resolved the request during the auto-fix; then, for a Continue, the waiting case's help key is rewritten with the summary).
- `CheckpointHelp` gains `ask` and `auto_fix_summary` (default `""`; older help keys and bad values parse to `""`). Continue and auto-continue both write the help key from the request (`case_help_services._write_help_key`): ask, the person's note (empty for auto-continue), the summary.
- `orchestrator_prompts.format_help_state_line` / `help_verdict_note`: "the test machine was fixed by <the machine setup agent | a person | a person (the machine setup agent could not)>", then "The agent had asked: <ask, or the need_help reason>", "Machine setup agent: <summary>", "Person: <note>" when present. `_applied_help` fills a missing ask from the decision reason for the verdict.
- Case page: the Continue form shows "What the Machine setup agent tried" above the note once an auto-fix has ended; Help history shows both texts. Autoescaped.

**F2. Change the help mode of an active run.**
- `projects/run_help_mode_services.change_run_help_mode(*, test_run, user, help_mode) -> HelpModeChange`: locks project → run; WAITING, STARTED or PAUSED only (`RunHelpError` otherwise); validates with `is_valid_help_mode`; same mode is a no-op.
- Applies from the next Need help. Exception: a PAUSED run whose open request has `auto_fix_state` none, switched to Night and `auto_fix_eligible`: state → running and, on commit, `services.enqueue_case_auto_fix` (renamed from `_enqueue_case_auto_fix`; same broker-failure fallback to not_fixed + notify), then broadcast. Switching to Attended never stops a running auto-fix.
- `TestRunStatusEvent` and the consumer's `current_state` / `test_run_status` gain `help_mode`, so open run pages follow the change without a reload.
- POST `projects:test_run_help_mode` (`.../test-runs/<id>/help-mode/`, `RunHelpModeChangeForm`, required) redirects to the run page with a message.
- Run page header: for a running or paused run, a "Help mode" select + Save (`.help-mode-form`, `.select-sm`); Save is disabled until the pick differs. A draft keeps its Start radio; a finished run shows the mode as text.

## Decisions (answered 2026-10-24)

- Q1. Notify every project member who enabled notifications on at least one device.
- Q2. Mark failed and Skip are offered. Skip = Blocked "Skipped while waiting for help", resumable later. Abort works on a sleeping run.
- Q3. No expiry and no reminder; the run sleeps until someone acts or aborts. In Night mode the auto-fix runs first.
- Q4. Need help immediately for environmental problems. Recovery only for the agent's own mistakes or a still-running long operation. A failed non-product recovery escalates to Need help.
- Q5. **Replaced**: a per-run help mode, Attended or Night, chosen at Start and kept on the run; no global switch. Resume and Re-run in place keep the mode (changeable in their dialog); a re-run in a new run copies it as the Start default.
- Q6. The Machine setup chat may be used while a run sleeps. Continue waits while any setup turn is live; other runs stay blocked.
- Q7. Orchestrator decision only; the executor fails fast with an `ENVIRONMENT:` summary.
- Q8. Infrastructure faults stay Blocked and use Resume.
- Q9. iPhone/iPad supported through the manifest and Add to Home Screen (iOS 16.4+), with an on-page hint.
- Night auto-fix: no guardrails, budget or cap beyond Machine setup's existing ones (user decision). It reuses the setup chat, turn and sub-task machinery in a new visible thread, and reports through `report_case_fix`.

- Q10. The same step failing again after a reported auto-fix goes straight to a person (push, wait). Each step gets at most one auto-fix per from-scratch execution of the case; other steps can still get their own.

## Open questions

None.

## Changelog

- 2026-10-24: created (Draft).
- 2026-10-24: Q1–Q9 answered with the defaults. Q5 was replaced by a per-run help mode (Attended / Night), and the global `HUMAN_HELP_ENABLED` setting was dropped. Night auto-fix was added: a new `auto_fix` Machine setup thread reusing the existing turn machinery and limits, a `report_case_fix` tool, `finish_case_auto_fix` settling to auto-continue or the Attended fallback, and human actions during the auto-fix. `SetupConversation.kind` / `help_request` / `fix_result`, `SetupTurn.auto_fix`, `TestRun.help_mode` and `CaseHelpRequest.auto_fix_state` were added. Q10 (the auto-fix loop limit) was raised; the plan stays Draft until it is answered.
- 2026-10-24: Q10 answered (go to a person). Status In progress.
- 2026-10-24: follow-ups F1 (setup agent summary and person note both reach the tester; `CaseHelpRequest.auto_fix_summary`, `CheckpointHelp.ask` / `auto_fix_summary`) and F2 (change the help mode mid-run, `change_run_help_mode`, `projects:test_run_help_mode`, `help_mode` on the run status event).
- 2026-10-24: review and verification fixes. (1) Periodic `machine_setup.tasks.sweep_stale_auto_fixes` (`machine_setup/services/sweep_stale_auto_fixes.py`, `CELERY_BEAT_SCHEDULE` every 120 s, installed by the DatabaseScheduler; `execution` queue) settles stale auto-fix turns of projects with a PAUSED run whose request is `running`, and settles a request `running` with no thread for 15 min (`AUTO_FIX_START_GRACE`, measured from the new `CaseHelpRequest.auto_fix_started_at`, migration `projects/0029`) as not fixed; the projects case view no longer writes on GET (`settle_stale_auto_fix_turn` now takes a project id). (2) Abort settles a running auto-fix with no live turn as not_fixed; `get_run_help_state` reads only the run's latest request. (3) `send_push` moved to a dedicated `notify` queue (route, decorator, `notify-celery` compose service with concurrency 2, CLAUDE.md and example.env commands). (4) The staleness rule is `machine_setup.turn_state.auto_fix_turn_live` (reuses `SETUP_TURN_STALE_AFTER`). (5) `RunHelpState`, `get_run_help_state`, `enqueue_case_auto_fix` (and `mark_auto_fix_running`) moved to the leaf `projects/case_help_state.py`. (6) `stop_auto_fix` with no turn notifies on commit. (7) `queue_turn.broadcast_and_enqueue` isolates its broadcasts so they never block the enqueue; an enqueue failure closes the turn and the auto-fix settles not fixed. (8) `CheckpointHelp.auto_fix_fixed` (lenient parse, written by `_write_help_key` from `auto_fix_state`); `helper_name` words: setup agent; setup agent (a person also added a note); a person; a person (the machine setup agent could not). (9) `auto_fix_eligible` also counts a request whose auto-fix thread reported `fix_result=fixed` (same single query, joined through `setup_conversations`). (10) At most 20 push subscriptions per user (`MAX_SUBSCRIPTIONS_PER_USER`); the oldest are evicted in the save's transaction. Accepted risk, not fixed: untrusted case and screen text reaching the unattended setup agent (see Risks).
- 2026-10-24: second review pass. (1) The sweep also settles a `running` request whose auto-fix thread has no live turn: under the project lock it re-checks `running` and `auto_fix_turn_live`, skips a turn that ended within `AUTO_FIX_SETTLE_GRACE` (2 min), and calls `settle_case_auto_fix` with the latest ended turn (so a reported fix counts), or `finish_case_auto_fix(fixed=False)` when the thread has no turn. (2) The sweep task is routed to the `notify` queue (route and decorator). (3) `finish_case_auto_fix` on an open request whose `auto_fix_state` is no longer `running` (Stop before a turn existed, or an earlier settle) only records `auto_fix_summary`: no second push, no auto-continue; a request a person resolved during the auto-fix still queues the pending cases. (4) The sweep's single candidate query also takes requests of paused runs with a live auto-fix turn whatever their state, so a turn row left queued by a failed enqueue close is closed once stale; the `_enqueue` comment says so.
