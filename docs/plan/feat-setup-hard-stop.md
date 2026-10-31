# feat: Force stop for a machine setup turn

Status: Done
Created: 2026-10-31
Request: Add a hard stop ("Force stop") to the machine setup chat. Today Stop only
sets `SetupTurn.stop_requested`, and the turn ends only when its Celery worker
notices the flag at its next iteration or tool boundary. When that worker is gone
(a deploy recreated `execution-celery` mid-turn, as with project 24 / turn 448) or is
hung inside a blocking call (a controller action or LLM call that does not
return), nothing happens. The turn stays `running` until `SETUP_TURN_STALE_AFTER`
(about 67 min), and until then it blocks sends, run start/resume and machine setup
for the whole project. Force stop closes the turn in the database at once, under the
project lock, the same way the stale path does: it fails the running tool rows,
writes a notice, broadcasts, and settles an auto-fix. It also revokes the Celery task.
A worker that survives must not write anything to the turn or its conversation after
that. The same applies to all thread kinds (main chat, auto-fix, help chat, result
chat), since they share `SetupTurn`.

## Context

- Stop: `machine_setup/views.py::setup_stop` (POST `stop/`, `@project_membership_required`)
  calls `services/stop_turn.request_stop`, which takes the project row lock, then
  `live_turn.resolve_live_turn` and `live_turn.mark_stop_requested`. A queued turn is
  closed at once (`_close_queued_turn`). For a running turn only `stop_requested` /
  `stop_requested_by` are set. Reset (`services/reset_conversation.py`) also uses
  `mark_stop_requested(reset=True)` and archives the chat. Auto-fix stop
  (`machine_setup/auto_fix_stop.py`) sets the flag with no broadcast.
- Lost worker: `live_turn.resolve_live_turn` closes a turn as `failed/worker_lost`
  only once `turn_state.is_turn_stale` is true. `_close_lost_turn` already does
  everything force stop needs (conditional UPDATE on `LIVE_TURN_STATUSES`,
  `activity_recorder.fail_running_rows`, a NOTICE row, an on-commit broadcast,
  `auto_fix_settlement.settle_case_auto_fix`), with a hard-coded status, reason and text.
- Gates: `turn_state.live_turn_exists` / `auto_fix_turn_live` / `person_turn_live`
  filter by status (plus not stale). A force-closed turn stops gating as soon as the
  transaction commits.
- Worker: `machine_setup/tasks.py::run_setup_turn` runs on the `execution` queue
  (compose: prefork, `--concurrency=10`), `acks_late=True`,
  `reject_on_worker_lost=True`, soft/hard limits from `turn_state`.
  `services/run_turn.py::run_setup_turn` claims only `QUEUED` turns, so a redelivered
  message is a no-op once the row is closed. `_TurnRunner`:
  - `_should_stop` (cancellation check, also shared with sub-tasks via
    `SetupTaskRuntime.cancellation_check` and with tools such as `wait`/`wait_for_command`
    polling in `agents/services/tools_utility.py` / `tools_controller.py`) looks at
    `stop_requested` or an archived conversation. It ignores the turn status.
  - `_on_transcript` writes display rows (`ActivityRecorder.on_message`) and
    **unconditionally** rewrites `SetupConversation.llm_transcript`.
    `agents/services/agent_loop.py` calls it through `_append(response.message)`
    *before* the post-response `_check_cancellation`.
  - `_finish` **unconditionally** closes the transcript (`_close_transcript`, which
    rewrites `llm_transcript`), fails running rows, writes the outcome notice, and
    then does a conditional `status=RUNNING` UPDATE. After that it **always** calls
    `broadcast_turn` with its own in-memory outcome, even when the UPDATE matched 0
    rows. Only settle/notify are gated on `ended`.
- Client: `templates/machine_setup/chat.html` `applyTurn` replaces `this.turn` with
  any payload of the shown conversation, with no turn-id ordering.
  `includes/_composer.html` renders the Stop form (`x-show="turnLive"`, disabled and
  showing "Stopping…" once `turn.stop_requested`). A turn of an archived conversation
  (after Reset) only shows as `finishingPreviousTurn`, with no control at all.
- `turn_payload` (`machine_setup/payloads.py`) carries `stop_requested` and no stop time.
- Versions: Django ~5.2.11, celery[redis] ~5.6.2, channels ~4.3.2.

## Goals
- A "Force stop" control on the chat page (all thread kinds) that ends a running,
  already-stop-requested turn at once and unblocks the project.
- After the force close, a surviving worker must not change the turn row, its
  conversation transcript, or its chat rows, and it must not start new machine actions.
- Best-effort termination of the worker process that runs the turn, freeing the
  execution slot.
- The next turn can start right away and replays a valid transcript.

## Non-goals
- Undoing actions already sent to the Test Runner. An in-flight `click` or a command
  started by `execute_command` keeps running on the machine, as with Stop.
- A controller-side "abort current action" protocol message (no `ClientCapability`
  change).
- Shortening `SETUP_TURN_STALE_AFTER` or detecting orphaned turns after deploys (see
  Follow-ups).
- Force stop for test-run case workers (`projects` execution tasks).

## Design

### Data model
- `SetupTurnEndReason.FORCE_STOPPED = "force_stopped", "Force stopped"`. Status stays
  `SetupTurnStatus.STOPPED`, so every status-based gate, the `stopped` badge and
  `REPLY_END_STATUSES` work unchanged. A separate reason keeps the audit trail and
  gives the auto-fix settle summary "ended (force stopped)".
- `SetupTurn.stop_requested_at = DateTimeField(null=True, blank=True)`. It is set
  wherever `stop_requested=True` is written (`mark_stop_requested`,
  `_close_queued_turn`, `auto_fix_stop.request_auto_fix_stop`) and is needed for the
  grace period. `null` with `stop_requested=True` (rows flagged before the deploy)
  counts as "long ago", so force stop is allowed immediately.
- One migration `0008_turn_force_stop`: `AddField` (nullable, so adding it in Postgres
  is a metadata-only change and does not rewrite the table) plus `AlterField` on
  `ended_reason` choices (state only, no SQL). No data migration. It is safe on the
  populated table.

### Services
- `machine_setup/services/live_turn.py` (refactor, no behaviour change for existing
  callers):
  - Generalise `_close_lost_turn` into a public
    `close_live_turn(turn, now, *, status, ended_reason, error_message, notice_text, require_status=LIVE_TURN_STATUSES) -> bool`.
    It does the conditional UPDATE, fail_running_rows, NOTICE row, on-commit
    broadcast and settle_case_auto_fix, and returns whether it closed the turn.
    `resolve_live_turn` calls it with the worker-lost values.
  - `mark_stop_requested` / `_close_queued_turn` also write `stop_requested_at=now`.
- New `machine_setup/services/force_stop_turn.py` (use case):
  `force_stop_turn(*, project: Project, user: CustomUser, turn_id: int) -> None`.
  In one `transaction.atomic`:
  1. Lock the project row (the lock every send/stop/reset path takes).
  2. `resolve_live_turn` (so a stale turn still closes as worker_lost first).
  3. Load the turn by `pk=turn_id, project=project` with `select_for_update()`. This
     serialises with the worker's `_finish` (below).
  4. Refuse with domain errors: not live → `SetupNoActiveTurnError` (info message);
     not `stop_requested` → `SetupForceStopNotAllowedError("Press Stop first.")`;
     inside the grace period → same error, with the seconds left. A queued turn never
     gets here, because Stop already closed it.
  5. `close_live_turn(status=STOPPED, ended_reason=FORCE_STOPPED, error_message="", notice_text=FORCE_STOPPED_NOTICE)`
     with `require_status=(RUNNING,)`. The notice reads: "Force stopped. The agent's
     last action may still finish on the test machine; check it before carrying on."
  6. Register `transaction.on_commit` to revoke the Celery task (see Background work)
     when `celery_task_id` is non-empty. The revoke is wrapped so that it never raises
     and failures are only logged.
  - No `notify_turn_reply`: the person pressed the button. `stop_requested_by` is not
    overwritten, and the actor is logged at INFO (project, turn, user id). A separate
    `force_stopped_by` column is not added (see Open question Q5).
- `machine_setup/exceptions.py`: `SetupForceStopNotAllowedError(MachineSetupError)`.
- Worker guard in `machine_setup/services/run_turn.py`. The invariant: every write to
  the turn row or the conversation transcript happens only while the turn is
  `RUNNING`, and the final end write is atomic with that check.
  - `_should_stop` also returns true when the turn is no longer `RUNNING` (same single
    query: `Q(stop_requested) | Q(archived) | ~Q(status=RUNNING)`). Every iteration,
    tool boundary, sub-task loop and `wait`/`wait_for_command` poll then exits once the
    row is closed, whether by force stop or worker_lost.
  - `_on_transcript` first checks that the turn is still `RUNNING` (one PK query per
    message) and raises `AgentCancelledError` otherwise, before any recorder write or
    transcript persist. The sub-task's `on_transcript` lambda in `run_setup_task.py`
    does the same through a `turn_live` callable added to `SetupTaskRuntime`. This
    covers the "hung LLM call returns after force stop" path, where `_append` runs
    before the loop's own cancellation check.
  - `_persist_transcript` becomes conditional: the conversation UPDATE is filtered on
    "this turn is RUNNING" (`Exists(SetupTurn pk=…, status=RUNNING)`), so a late writer
    cannot overwrite the transcript a newer turn has started.
  - `_finish` is reordered inside `transaction.atomic()`: `select_for_update` the turn
    row; if it is not `RUNNING`, log and return with no transcript close, no notice, no
    broadcast, no settle and no notify (whoever closed it did those). Otherwise do the
    current steps (close transcript, fail rows, notice, status UPDATE). Broadcast,
    settle and notify run after commit, as today. The transcript close stays before the
    status change, so a new turn can never start before the old transcript is closed.
    Because force stop locks the same turn row, exactly one of them writes the end
    state.
  - Known residual window: a display row or screenshot written between the live check
    and the force-stop commit. At most one row lands after the notice. It is
    harmless and is closed by `fail_running_rows` from `_finish` if it was a running
    tool row. This is accepted rather than locking every row write.
- Transcript after a force stop: the last persisted transcript ends at most with an
  assistant message whose tool calls have no results. The next turn's
  `close_dangling_tool_calls(history, _INTERRUPTED_CALL_NOTE)` already answers them, so
  no change is needed.

### Pages and forms
| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `projects/<id>/machine-setup/force-stop/` (`machine_setup:force_stop`) | POST | `@project_membership_required` (read-only → 403) + `@require_POST` | `SetupForceStopForm`: `turn_id` (required int), `conversation_id` (optional, for the redirect, as `SetupThreadForm`) | Redirect back to the chat/thread (`_back_to_chat`) | Invalid form → error message; `SetupNoActiveTurnError` → info; `SetupForceStopNotAllowedError` → error |

- `turn_id` is required so a stale page can never force-stop a newer turn it has not
  shown.
- No throttle. Stop, Reset and Send are not throttled, and throttles are only used for
  credential and runner surfaces. The action is idempotent under the project lock and
  is preconditioned on a prior Stop.
- `turn_payload` adds `force_stop_in: int | None`: seconds until Force stop is allowed
  (0 = now). It is `None` unless the turn is `running` and `stop_requested`. It is
  computed server-side when the payload is built, so client clocks do not matter.
  `SetupTurnPayload` TypedDict is updated to match.
- `_composer.html`: while `turnLive && turn.stop_requested`, the Stop button (disabled
  "Stopping…") is joined by a second form posting to `force_stop` with
  `turn_id=turn.id` (bound with `:value`). The Force stop button is shown when the
  client countdown (started from `force_stop_in` on each payload) reaches 0. It opens a
  confirm dialog using the `dialog()` convention in `templates/partials/README.md`. The
  dialog text: "Ends the turn now without waiting for the agent. The action in
  progress may still finish on the test machine." Text comes only from server
  constants; no `|safe`.
- `chat.html`:
  - `applyTurn` ignores a payload for the shown conversation whose `id` is lower than
    the current `turn.id`. This is defence in depth against late broadcasts.
  - The "working" text becomes "Stopping… (the agent is not responding)" once the
    countdown is over.
- Finishing-previous-turn case (Reset while a turn hangs): the main chat has no control
  today. Add the archived turn's payload to the page/`current_state`
  (`finishing_turn`, built in `services/chat_page.py` from the already-loaded
  `live_turn`, so no extra query) and store it from `applyTurn` for archived
  conversations. The blocker line then offers the same Force stop form. Reset always
  sets `stop_requested`, so the grace rule applies unchanged. See Q3.
- Help panel and case pages are unchanged. Auto-fix, help and result threads use the
  same composer through the thread page.

### Background work and WebSockets
- Revoke: `celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")` from
  the force-stop service's `on_commit`, in the web process. It is a fire-and-forget
  pidbox broadcast, with no reply wait. Behaviour per Celery 5.6 docs (workers guide,
  "Revoking tasks"):
  - `terminate` is supported by prefork (our execution worker), but not by solo/threads.
  - The default signal is TERM. In a prefork child, Celery resets SIGTERM to the
    default action, so the child dies at once. No Python cleanup runs, Postgres rolls
    back its open transaction, and billiard replaces the child.
  - The docs call terminate "a last resort for administrators when a task is stuck"
    and warn that the process "may have already started processing another task at
    the point when the signal is sent, so … you must never call this programmatically".
    In our setup the window is narrow:
    - The worker's main process only signals the pid of a request still in its
      `active_requests`.
    - The `fair` scheduling default plus `prefetch_multiplier=1` do not hand that child
      a new job before the main process has processed the finished one.
    - Force stop only happens after a Stop that went unanswered for the grace period.
    The risk is real but small, and it is the main reason for Q2.
  - Revoked ids are in-memory per worker (no `--statedb`). If the worker restarted (the
    deploy case), the revoke is a no-op. A later redelivery of the message
    (`acks_late`, redis visibility timeout 4 h) finds the turn not `QUEUED` and returns.
  - A terminated request is reported as `Terminated`, not `WorkerLostError`, so
    `reject_on_worker_lost` does not requeue it. Even if it did, the claim is a no-op.
- The force close does not depend on the revoke. The DB close plus the worker guard are
  the correctness mechanism. The revoke only frees the execution slot and cuts a hung
  call short.
- WebSocket events: the existing `message` (failed tool rows, notice) and `turn` (status
  `stopped`, `ended_reason=force_stopped`) events are sent on commit through
  `close_live_turn`. The payload shape changes only by the new `force_stop_in` key (and
  `finishing_turn` in `current_state`).
- Effects on a running sub-agent (`run_setup_task`): its rows are nested under the
  delegation row with the same `turn_id`. `fail_running_rows` fails them and marks the
  delegation `interrupted`. If the process survives, its loop exits at the next
  `cancellation_check` or `on_transcript` guard.
- Effects on the controller: an action already sent (`click`, `wait_for_command`
  polling the command) runs to completion on the runner. Its reply goes to a
  channel-layer reply channel nobody reads, and that channel expires. Commands the
  agent started keep running, as with Stop. The next turn starts once the commit lands,
  and the shared `setup_one_live_turn_per_project` constraint is satisfied because the
  row is `stopped`. The new turn's first action may meet the tail end of the
  interrupted action. The notice tells the person to check the machine, and the
  interrupted-call note in the transcript tells the agent the same.
- Auto-fix: `close_live_turn` calls `settle_case_auto_fix`, which reports "not fixed"
  ("ended (force stopped) without reporting a fix"), and the case falls back to a
  person. That is the same path as worker_lost, and the sweep stays consistent because
  the turn is no longer live.

### Settings / infrastructure
- `MACHINE_SETUP_FORCE_STOP_GRACE_SECONDS` (int, default 30; 0 = Force stop offered as
  soon as Stop was pressed), read in `settings.py` via `config(...)` and added to
  `example.env`.
- Why 30 s: controller actions default to a 30 s reply timeout
  (`projects/services.py` `controller_*`). A cooperative stop normally lands within one
  tool boundary, which lets the worker write its own clean end (transcript closed in
  order, rows finished). It is still short enough that a dead worker no longer blocks
  the project for an hour. A server-side check enforces it too, so a crafted POST
  cannot skip it.
- No worker or compose changes are required.

### Data flow
1. A member presses Stop. `stop_requested=True` and `stop_requested_at=now` are
   written, then broadcast. The payload has `force_stop_in=30`.
2. The page counts down and then shows Force stop. The member confirms and posts
   `turn_id`.
3. `setup_force_stop` validates the form and calls `force_stop_turn`. Under the project
   lock and the turn row lock it runs the checks, then `close_live_turn`: the turn
   becomes `stopped/force_stopped`, tool rows fail, a notice is written, and an
   auto-fix is settled.
4. On commit: the `message` and `turn` broadcasts go out, then the Celery revoke with
   TERM goes out.
5. The project gates open at once, and the composer shows Send.
6. If the worker is dead, nothing more happens. If it is alive, it is either killed by
   the revoke or exits at its next guard. Its `_finish` sees the row is not `RUNNING`
   and writes nothing.

## Alternatives considered
- **Make Stop itself close the turn at once.** This loses the clean cooperative end in
  the common case: the worker writes the final transcript and rows itself, and an
  in-flight sub-task report is kept. It would also race every normal stop against a
  live worker. Rejected in favour of a two-step control.
- **Reuse `STOPPED_BY_USER`.** No migration, but force stops become indistinguishable
  in data, logs and auto-fix summaries. The choices migration is state-only, so the new
  reason costs nothing.
- **Revoke with `SIGUSR1`** (raises `SoftTimeLimitExceeded` in the child, the same path
  as the soft time limit, and keeps the child alive). It is gentler, but it can be
  swallowed by tool-level `except Exception`, and it still needs the guard. If it hits
  the wrong task in the race window, it turns that task into a fake time-limit fault.
  TERM is decisive. This is offered as an option in Q2.
- **No revoke at all** (DB close + guard only). It is fully safe w.r.t. the docs'
  warning. A hung worker keeps one of 10 execution slots until its hard time limit
  (~66 min), but it no longer blocks the project. This is offered as an option in Q2.
- **Shorter stale timeout / heartbeat.** It does not help a hung-but-alive worker, and
  sub-tasks legitimately run up to 30 min between rows. It is a follow-up for orphaned
  turns, not a replacement.
- **Lock every display-row write.** One more locking query per row on the hot path, to
  close a window that can only leak one harmless row. Not worth it.

## Risks and mitigations
- **Terminate hits another task** (Celery doc warning): there is a narrow window. It is
  mitigated by the grace period, the precondition on Stop, a revoke sent only for the
  turn's own task id, and the Q2 setting/choice. The force close itself never depends
  on it.
- **Late worker writes** (transcript clobbering a newer turn, a stale `turn` broadcast
  re-showing an old turn as current): fixed by the conditional transcript UPDATE, the
  locked `_finish`, the `on_transcript` guard and the client's id ordering.
- **Machine left mid-action**: unavoidable without a controller abort action. The
  notice and the interrupted-call note tell the person and the agent.
- **Migration on a populated table**: a nullable AddField plus a choices-only AlterField
  cause no rewrite and no data loss.
- **Auth**: a POST-only view behind `@project_membership_required`, so read-only
  members get 403. `turn_id` is scoped by `project=project`, so a foreign id returns
  `SetupNoActiveTurnError`, never another project's turn.
- **XSS**: the new texts are server constants rendered with auto-escape. `x-text` is
  used for dynamic labels.
- **N+1 / queries**: force stop adds about 6 queries once per press. The worker guard
  adds one PK query per transcript message and folds the status into the existing
  `_should_stop` query.

## Implementation steps
1. Model and migration: `FORCE_STOPPED` reason, `stop_requested_at`, `0008_turn_force_stop`.
   Files: `machine_setup/models.py`, `machine_setup/migrations/0008_turn_force_stop.py`.
2. Settings: `MACHINE_SETUP_FORCE_STOP_GRACE_SECONDS`. Files: `auto_tester/settings.py`,
   `example.env`.
3. `live_turn` refactor (`close_live_turn`), `stop_requested_at` writes, and auto-fix
   stop writes. Files: `machine_setup/services/live_turn.py`,
   `machine_setup/auto_fix_stop.py`.
4. Worker guard: `_should_stop` status clause, `on_transcript` live check (main and
   sub-task via `SetupTaskRuntime.turn_live`), conditional `_persist_transcript`, locked
   `_finish` that skips everything when it is not `RUNNING`. Files:
   `machine_setup/services/run_turn.py`, `machine_setup/services/run_setup_task.py`.
   This step can be reviewed on its own, because it also fixes late writes after a
   worker_lost close today.
5. Use case and exception: `force_stop_turn` with the revoke on commit. Files:
   `machine_setup/services/force_stop_turn.py`, `machine_setup/exceptions.py`.
6. Payload: `force_stop_in` in `turn_payload` / `SetupTurnPayload`, and `finishing_turn`
   in the page context and `current_state`. Files: `machine_setup/payloads.py`,
   `machine_setup/services/chat_page.py`, and `machine_setup/consumers.py` if it builds
   `current_state`.
7. Form, view and URL: `SetupForceStopForm`, `setup_force_stop`, and
   `path("force-stop/", …, name="force_stop")`. Files: `machine_setup/forms.py`,
   `machine_setup/views.py`, `machine_setup/urls.py`.
8. UI: Force stop form, confirm dialog, countdown, id-ordered `applyTurn`, and the
   finishing-turn control. Files: `templates/machine_setup/includes/_composer.html`,
   `templates/machine_setup/chat.html`, and any button/dialog class needed in
   `static/css/punk-hazard.css`.
9. Docs: `docs/changed/2026-10-31-setup-hard-stop.md`, plus a line in `CLAUDE.md`
   (Need help / machine setup section) on Force stop and the worker guard.

Steps 1–3 come first (shared). Steps 4, 5 and 6–8 touch disjoint files and can go to
parallel sub-agents.

## Verification
- `mypy .`, `pre-commit run --all-files`, `python manage.py makemigrations --check`
  after the migration, and `python manage.py check`.
- Manual (dev: Daphne on a worktree port, execution worker with prefork):
  1. Start a turn that runs `wait_for_command` on a long command. Press Stop and see
     "Stopping…" plus the countdown. Press Force stop when shown. The turn turns
     `Stopped` with the notice, the running tool rows fail, and Send is enabled at once.
     The worker log shows the task terminated (`Terminated`), and the next turn runs and
     sees the interrupted-call note.
  2. Dead worker: start a turn, `kill -9` the execution worker (or stop it). Stop, then
     Force stop after the grace period. The turn closes, and the run Start gate passes
     (`live_turn_exists` is false).
  3. Late worker: with revoke disabled (Q2 option / a temporary local edit not
     committed), force stop during a slow LLM call. When the call returns, no new
     rows, no transcript change (compare `llm_transcript` before and after) and no
     extra `turn` broadcast appear.
  4. Grace: a POST before the grace period ends gives an error message. A POST without
     a prior Stop is refused. A POST with an old `turn_id` after a new turn started is
     refused.
  5. Read-only member: the POST returns 403.
  6. Auto-fix thread: force stop the Night auto-fix turn. The help request falls back
     to a person with summary "ended (force stopped) …".
  7. Reset while hung: the main chat shows Force stop in the finishing-previous-turn
     line, and it works.
  8. Help chat and result chat threads: force stop works, and the help request / result
     card is unchanged.
- Run `reviewer` and `verifier` (with this plan and the original request).

## Open questions
- Q1 Grace period: Force stop offered 30 s after Stop (setting
  `MACHINE_SETUP_FORCE_STOP_GRACE_SECONDS`), immediately, or a different value? (Draft
  assumption: 30 s, server-enforced, 0 allowed.)
- Q2 Worker termination: revoke with `terminate=True, signal="SIGTERM"` (decisive;
  Celery docs warn the signal can hit another task in a narrow race), `SIGUSR1` (soft,
  may be swallowed), or no revoke (DB close + guard only; a hung worker holds one
  execution slot up to ~66 min)? (Draft assumption: SIGTERM, always on, no setting.)
- Q3 Should Force stop also be offered on the main chat for a turn of a chat that was
  Reset (today only "finishing the previous turn" is shown, with no control)? (Draft
  assumption: yes, step 8.)
- Q4 Who may force stop: any writable member, or only the member who pressed Stop /
  sent the turn? (Draft assumption: any writable member, like Stop.)
- Q5 Record who force-stopped in a column (`force_stopped_by`) for audit, or is an
  INFO log line enough? (Draft assumption: log only; `stop_requested_by` keeps who
  pressed Stop.)
- Q6 Notify (ntfy "missed reply") the turn's sender when someone else force-stops it?
  (Draft assumption: no notification, matching Stop.)

## Follow-ups (out of scope)
- Deploys orphan live turns: recreating `execution-celery` SIGKILLs prefork children
  after Docker's stop timeout, leaving `running` turns until the 67-min stale window.
  The same applies to running test cases. Options for a separate plan:
  - Record the worker hostname on claim (`self.request.hostname`, with a stable `-n`
    and compose `hostname:`). On `worker_ready`, close that host's `RUNNING` turns as
    worker_lost.
  - Or, from `worker_shutting_down`, set `stop_requested` on its turns and raise
    `stop_grace_period` in compose so warm shutdown lets them end cleanly.
  - Or have the deploy script close live turns before recreating the worker.
  Force stop is the manual escape hatch until then.

## Decisions (2026-10-31)
- Q1 30 s grace (setting, 0 allowed). Q2 revoke with SIGTERM, always on. Q3 yes,
  Force stop on the finishing-previous-turn line. Q4 any writable member. Q5 log
  only. Q6 no notification.

## Changelog
- 2026-10-31: created
- 2026-10-31: answers recorded. Production observation (project 29, turn 456):
  `revoke(terminate=True, signal="SIGTERM")` on a worker hung in a controller wait
  did not kill the child silently: the turn's own `_finish` ran and closed it as
  `failed/internal_error`. The locked, status-checked `_finish` therefore matters:
  after a force close it must write nothing.
- 2026-10-31: review round 1. Fixed: guarded late screenshot and sub-task result
  writes; `_count_iteration` broadcasts only when its guarded update hit the
  running turn and re-reads the stop fields first (a stale payload cleared the
  countdown); dropped a redundant view branch. Accepted, not changed: the Celery
  terminate race (the pool child may have started another `execution` task, e.g.
  a test case, by the time TERM lands) — narrow, Force stop is preconditioned on
  an unanswered Stop plus the grace period, and redelivered setup tasks are
  no-ops; a dedicated setup-turn queue/worker is the longer-term fix. Accepted:
  the `_count_iteration` broadcast race between the live check and the force
  close commit; the next broadcast corrects the page.
