# feat: Test Runner person window v2 (readable, full case, log, Pause, discussion)

Status: Ready
Created: 2026-10-31
Supersedes: `docs/plan/feat-tester-console.md` (web console, dropped by the user)
Depends on: `docs/plan/feat-pause-and-discuss.md` (Ready). It supplies the pause service, the runner pause seam, the first-message auto-send and the continue modes. This plan owns the runner protocol for pause and supersedes that plan's proposed protocol names (see "Pause from the runner").
Request: Testers find the Test Runner's native Tk person window hard to use: the text is small and cannot be copied, and most of what the web case page offers is missing. The user decided against a web console because only the runner can take screenshots, so the native window itself must get better. It must gain:
- big, selectable, copyable text everywhere, with an A−/A+ size control and a Copy button on every block;
- the original test case (preconditions, steps, expected results), with the TestRail steps the current step covers highlighted;
- the case log: recent lines, scrollable and copyable, refreshed with snapshots and bounded;
- a Pause button for the case, with an optional first message that is sent automatically as the first discussion turn once the case has stopped;
- the discussion chat: talk to the agent about the paused case (the `case_help` `SetupConversation` thread) inside the window. That includes sending turns, receiving new messages, and seeing the agent's proposals (plan change and resolution), with Continue and a link to Punk Hazard.

All rules stay in server services shared with the web case page; the consumer holds no business logic. There is a CLIENT_VERSION bump with new optional capabilities, and older runners degrade gracefully. The window stays hidden during agent actions, never takes focus, and stays fast with long logs and chats. The one-tap Same / Different report applies to the window.

## Context

- **Window today** (CLIENT_VERSION `0.10.3`):
  - `controller_client/person_window_app.py` is one scrollable page: header, step picker, Covers, What to do, Expected, command, How to do it, an As expected / Different radio and a Report button, note, output, Ask the AI, and a "Case page" URL row.
  - Text uses Tk default fonts at `_WRAP = 480`. Several blocks are `ttk.Label`s, which cannot be selected. Copy exists only for the command and the URL.
  - `person_window.py` (manager) keeps the latest snapshot and decides visibility:
    - the window wants to be visible while an unanswered step exists;
    - it is hidden with an ack before every agent action (`hidden_for_action`);
    - it re-shows after 5 s of quiet (30 s on macOS unless something is new).
  - `person_window_ipc.py` carries JSON lines, capped at 4 MiB towards the window.
- **Server side**:
  - `projects/runner_person_step_services.get_runner_person_steps` builds the snapshot in 1 query, with no blockers.
  - `answer_from_runner` / `ask_from_runner` check that the answer comes from the project's owner connection, apply throttles (20 answers and 10 questions per minute per project) and resolve the actor (`projects/person_step_actor.PersonStepActor`: a web user, a member resolved from the runner's email, or an anonymous runner labelled "a person at the Test Runner").
  - `projects/controller_consumer.py`: `person_email` at handshake, deduplicated snapshot pushes on `controller.person_steps_changed` nudges (`projects/runner_person_step_push.py`), and the `person_step_action` / `person_step_question` handlers (form → service → result).
- **Help chat**:
  - `machine_setup/services/open_help_chat.open_help_chat(project, pivot, user, text)` creates or reuses the request's `case_help` thread and sends the first turn.
  - `machine_setup/services/send_message.send_setup_message(project, user, text, conversation)` refuses while a turn is live, while a run is Started or while the machine is not ready.
  - `SetupMessage.author` and `SetupTurn.requested_by` are nullable FKs. `machine_setup/payloads.message_payload` gives `author_name` from the FK.
  - `HelpThreadPayload` (`machine_setup/payloads.py`, via `help_thread_state.load_help_thread_payload`) already shapes the agent proposal (`action_label`, `note`) and the plan draft (`PlanDraftPayload`) for the thread page.
  - Events go to `machine_setup.events.setup_group(project_id)`.
- **Help resolution**: `projects/case_help_services.continue_after_help(pivot, user, note, plan_revision_id, use_plan_revision)`, `mark_failed_after_help` and `mark_blocked_after_help` take `user: CustomUser`. `_resolve` writes `resolved_by`, credits and records test-session activity.
- **Pause** (`docs/plan/feat-pause-and-discuss.md`, Ready):
  - `projects/case_pause_services.request_case_pause(*, pivot, actor: PersonStepActor, message="")`.
  - `get_case_pause_panel(test_run, pivot)` (no queries): `can_pause`, `pause_blocker`, `pause_pending`, `pause_requested_by_label`, `paused`, `paused_by_label`.
  - The runner seam `projects/runner_case_pause_services.pause_from_runner(*, project_id, channel_name, person_user_id, pivot_id, message)`. It enforces the owner connection, applies the shared per-project runner answer throttle (20/min), and builds the actor `PersonStepActor.runner(member or None)`.
  - The first message is stored with the pause and sent after commit by `machine_setup.tasks.open_paused_help_chat`, with a `pause_message_state` that includes the refused / dropped outcomes. An anonymous runner pause gets `refused`, because a help-chat turn needs a member.
  - Continue modes on every help request: `case_pause_policy.allowed_continue_modes(stored, fault, checkpoint_json)`, `HelpPanel.continue_mode_choices` and `HelpPanel.agent_continue_mode` (the agent's `propose_resolution(continue_mode)` suggestion). The initial choice is the stored mode. A redesign requires `current_screen` and is refused on a restore request.
  - The pause plan proposes runner messages `case_pause_action` / `case_pause_action_result` and capability `CASE_PAUSE_V1`, and leaves the protocol to this plan.
- **Import rule**: `projects.services` must never import `machine_setup.services`. Today no `projects` module imports `machine_setup.services` at all; only the leaf modules `machine_setup.models`, `task_names`, `auto_fix_stop` and `turn_state` are used.

## Goals
- Large, selectable, copyable text everywhere in the window, A−/A+, and Copy on every block.
- The window shows the case behind the step: the original test case with the covered steps highlighted, and a bounded log tail.
- Pause from the window, with an optional first message, for a waiting person step, and for a running case while the window is visible.
- The discussion (`case_help` thread) inside the window: read the last messages, send turns, see whether a turn is live, see the agent's resolution proposal and plan redesign, and act: Continue (mode, use the redesigned plan, note), Mark failed, Mark blocked, or open it on Punk Hazard.
- The same server rules, actor labelling, owner-connection check and throttles as person-step answers.
- Runners older than 0.11.0 keep today's window unchanged.

## Non-goals
- The web tester console, QR / `segno`, ntfy link changes and web keyboard shortcuts (all dropped).
- The result chat (`case_result`) in the window, run proposals and the main machine-setup chat.
- Screenshots in the window. Evidence is covered by `docs/plan/fix-hitl-failure-evidence.md`.
- Mark for re-test, Edit, Check and Send to TestRail from the window.
- Streaming token-by-token chat. Messages arrive per stored row.

## Design

### Data model
- `machine_setup.SetupMessage.author_label`: `CharField(max_length=255, blank=True, default="")`, server-written. It is set for Test Runner sends: "<member> via Test Runner", or "a person at the Test Runner" for anonymous sends.
  - `message_payload.author_name` prefers it over the FK.
  - Migration `machine_setup.00xx_message_author_label`, additive.
- `projects.CaseHelpRequest`, so a resolution from the window is labelled like a person-step answer:
  - `resolved_via`: `PersonStepChannel` choices, default `web`;
  - `resolved_by_label`: CharField 255, blank;
  - migration `projects.00xx_help_resolved_via`, additive.
  - `resolved_by` stays null for an anonymous runner. The history and help card show the label when `resolved_by` is null.

### Actor generalisation (shared services, web behaviour unchanged)
- `case_help_services.continue_after_help` / `mark_failed_after_help` / `mark_blocked_after_help` take `actor: PersonStepActor` instead of `user`.
  - The web views pass `PersonStepActor.web(request.user)`. `_resolve` writes `resolved_by=actor.user`, `resolved_via` and `resolved_by_label=actor.label()`.
  - Credit and `record_case_activity` run only when `actor.user` is set; anonymous runner answers are not recorded, as in `person_step_services`.
  - Every existing rule (locks, stale-card fingerprint, `continue_mode` validation from the pause plan) is unchanged.
  - All callers need updating: `projects/views.py` help views, `machine_setup` thread help-panel views, and any service that resolves on a person's behalf.
- `machine_setup.services.send_message.send_setup_message` and `open_help_chat.open_help_chat` take `actor: PersonStepActor` instead of `user`.
  - `requested_by` / `author` = `actor.user`, `author_label` = `actor.label()` when the actor is a runner.
  - The web views pass `PersonStepActor.web(user)`.
  - Turn-reply notifications (`turn_reply_notification`) skip a turn with no requester; check this during implementation.
  - `PersonStepActor` is a plain projects module that `machine_setup` may import. Its name stays, and its docstring is widened to "a person acting on a run case".

### Pause from the runner (uses the pause plan's seam; this plan owns the protocol)
- The window's Pause calls `projects/runner_case_pause_services.pause_from_runner` (pause plan) unchanged. This plan adds no pause service and no own pause throttle: the pause plan's shared 20/min runner answer bucket applies, which help actions also use.
- Protocol: this plan **adopts** the pause plan's proposed names, `case_pause_action` {`request_id`, `pivot_id`, `message` ≤ `PAUSE_MESSAGE_MAX_LENGTH`} and `case_pause_action_result` {`request_id`, `ok`, `outcome`, `message`}, with their own optional capability `CASE_PAUSE_V1`. These definitions in `controller_client/protocol.py`, owned by this plan, are the only ones. A runner may advertise `CASE_PAUSE_V1` without `PERSON_DISCUSSION_V1`, but 0.11.0 advertises both together with the window.
- The first message goes through the pause plan's `open_paused_help_chat`.
  - **Anonymous runner pauses**: the pause plan currently refuses the auto-send ("a help-chat turn needs a member user"). Step 1 here generalises `open_help_chat` / `send_setup_message` to a `PersonStepActor` with `author_label`. With that in place, step 6 changes `open_paused_help_chat` to send the message with the request's runner label ("a person at the Test Runner"), as chat sends from the window do.
  - Until step 6 lands, the window shows the pause plan's `refused` state: "Your message was not sent; type it in the Discussion tab."
### Server services for the runner (new; all rules delegated)
- **`projects/runner_case_services.py`** (imports `projects` only). Read side: the window's case cards for a project.
  - `get_runner_cases(project, *, discussion: bool) -> tuple[RunnerCaseView, ...]`. It covers the cases of the project's active run that the window may show, at most 2:
    - the case of an open waiting person step (also referenced by the step entries);
    - the case of an open help request that waits for a person: run PAUSED, request open, no live Night auto-fix (`case_help_state`);
    - the IN_PROGRESS case, for the Pause button only; it never makes the window show.
  - Each view carries:
    - the pivot id, run name and case title;
    - the test case's preconditions, steps and expected text, each capped at 8000 characters;
    - `numbered_steps` (`case_step_refs.numbered_case_steps`, ≤ 50) for highlighting;
    - `log_tail`, the last ≤ 200 lines and ≤ 16 000 characters, computed in SQL with `Right("logs", 16000)` and trimmed to whole lines in Python, so the full log column is never loaded;
    - the pause panel fields (`get_case_pause_panel`);
    - with `discussion`, the help part: `help_request_id`, reason / ask, `fault_label`, `paused_by_label`, `continue_mode` (stored), `continue_mode_choices` (`HelpPanel.continue_mode_choices`, i.e. `allowed_continue_modes`, display order), `agent_continue_mode` (the agent's suggestion or ""), `can_continue`, blockers, the agent proposal, and the pending plan draft (`PlanDraftView`, rows ≤ 30, texts capped).
  - Query budget: 1 query for the cases with their test case and `log_tail`, plus, when a help case exists, 1 for the request with its draft and 1 for the wake gates (`list_wake_blockers`, only while PAUSED with a help case). The gates are included because Continue needs them; the snapshot is pushed on state changes only, not on a timer. See Q3.
  - Write side (Pause is the pause plan's `pause_from_runner`, not here), with the owner check (the same as `answer_from_runner`, factored into a shared private helper in `runner_person_step_services`), a throttle, and the actor resolved as today:
    - `resolve_help_from_runner(project, connection, pivot_id, help_request_id, action, note, continue_mode, plan_revision_id, use_plan_revision)` calls `continue_after_help` / `mark_failed_after_help` / `mark_blocked_after_help` with the actor. It shares the 20-per-minute answers bucket. `help_request_id` is a stale-card guard: refused unless it is the case's open request.
- **`machine_setup/services/runner_discussion.py`** (imports `projects`; never imported by `projects.services` or `case_help_services`):
  - `get_runner_discussion(project, help_request_id) -> RunnerDiscussionView | None`: the request's thread, if any (1 query, `defer("llm_transcript")`), and its last ≤ 40 display rows, newest kept (1 query). Rows include user / assistant / notice and exclude tool rows; a tool row becomes a one-line "The agent ran: <task title>" notice. Texts are capped at 4000 characters. The view also gives `turn_live` (`turn_state.live_turn_exists`), `can_send` and `send_blocker` (from the same refusals `send_setup_message` applies, without sending), and the absolute thread URL (`conversation_url` + `absolute_url`).
  - `send_from_runner(project, connection, pivot_id, help_request_id, text)`:
    - owner check and actor as above; throttle 10 per minute per project;
    - with no thread yet, `open_help_chat(actor=..., text=text)`; otherwise `send_setup_message(actor=..., conversation=thread)`;
    - maps `MachineSetupError` / `RunHelpError` to a user-safe refusal.
- **`projects/runner_window_state.py`** (new, top of the graph, imported only by `controller_consumer`). It composes the snapshot from `get_runner_person_steps`, `get_runner_cases` and, for each help case, `get_runner_discussion`. It holds no rules.

### Controller protocol (`controller_client/protocol.py`, CLIENT_VERSION 0.10.3 → 0.11.0)
- New optional capabilities, advertised only with `PERSON_STEPS_V1` (added to `_PERSON_WINDOW_CAPABILITIES`):
  - `PERSON_CASE_CONTEXT_V1`: the snapshot's `cases` with the test case and log tail; one-tap report.
  - `PERSON_DISCUSSION_V1`: help cases in `cases`, `discussion`, and `person_chat_send` / `person_help_action`.
  - `CASE_PAUSE_V1` (name from the pause plan): the pause fields in `cases` and `case_pause_action`.
- `person_step_state` gains `cases: [PersonCaseStatePayload]` (≤ 3) for runners with `PERSON_CASE_CONTEXT_V1`. The help and discussion fields are sent only with `PERSON_DISCUSSION_V1`, and the pause fields only with `CASE_PAUSE_V1`.
  - Every text has a `*_MAX_CHARS` constant (test case blocks 8000, log tail 16 000, message 4000, ≤ 40 messages, plan rows ≤ 30) and is cut by the parser, as today.
  - A step entry gains nothing: it is already keyed by `pivot_id`.
- `case_pause_action` / `case_pause_action_result`, as in "Pause from the runner".
- New runner → server messages, each answered by `person_case_action_result` (`ok`, `message`, with the request_id echoed):
  - `person_chat_send` {`pivot_id`, `help_request_id`, `text` 1..8000};
  - `person_help_action` {`pivot_id`, `help_request_id`, `action`: `continue` | `mark_failed` | `mark_blocked`, `note` ≤ 8000, `continue_mode`? (must be one of the snapshot's `continue_mode_choices`; the service re-checks it with `allowed_continue_modes` under the locks), `plan_revision_id`?, `use_plan_revision`}.
- Parsers on both sides, with `ProtocolError` on bad types. Server forms validate again (`RunnerCasePauseForm` for `case_pause_action`, `RunnerChatSendForm`, `RunnerHelpActionForm` in `projects/forms.py`).
- Older runners do not advertise the capabilities, so the server never sends `cases` and refuses the new messages with today's unknown-message error. Their window and the web pages work as before.

### Consumer (`projects/controller_consumer.py`, glue only)
- Handshake: store the two new capability flags.
- `_push_person_steps` builds the snapshot through `runner_window_state` (or `get_runner_person_steps` alone for older runners) and keeps deduplication by equality.
- **Coalescing**: nudges arriving within 1 s of a push schedule one trailing push (one timer per connection), so a burst of chat or tool rows costs one recompute per second at most.
- New handlers, each followed by a forced push:
  - `case_pause_action`: form → `pause_from_runner(project_id, channel_name, person_user_id, ...)` → `case_pause_action_result`;
  - `person_chat_send` and `person_help_action`: form → service → `person_case_action_result`.
- **Nudges**:
  - `machine_setup.events.broadcast_message` / `broadcast_turn` / `broadcast_help` also call `nudge_runner_person_steps(project_id)` (the leaf push module; `machine_setup` → `projects` is the allowed direction).
  - Run and case broadcasts already nudge.
  - The pause plan's commit broadcasts nudge through `broadcast_run_and_pivots`.

### Window (`controller_client/person_window_app.py`, `person_window.py`, `person_window_ipc.py`)
- **Layout**: a header (case title, run, status badge text, **Pause…**, **A−/A+**) above a `ttk.Notebook`:
  - **Step**: today's content, with one-tap **Same as expected** / **Different…** buttons replacing the radios and Report. Different reveals the note and requires it; Overrule / Mark failed / Mark blocked stay.
  - **Test case**: preconditions, numbered steps with the covered ones tinted, and expected results.
  - **Log**: the tail, with "Showing the last N lines · Open on Punk Hazard".
  - **Discussion**: only for a help case. The chat, a box with **Send**, a "The agent is working…" state, a proposal card, a plan draft card, and Continue (mode radios from `continue_mode_choices`, pre-selected on the stored mode, with "The agent suggests: <mode>" beside the suggested one; "Use the redesigned plan" when there is a draft, which forces current screen and is disabled on a restore request with the service's reason), Mark failed, Mark blocked and **Open on Punk Hazard**.
  - When only a help case exists (no step), the window opens on Discussion.
- **Text**:
  - Every block is a read-only `tk.Text`: key presses break except copy and select-all; selection is allowed. It sits in a Copy row (`_ReadOnlyBlock` helper class).
  - Named fonts (`TkDefaultFont`, `TkTextFont`, `TkFixedFont`) are scaled together by A−/A+ from a base of `CONTROLLER_PERSON_WINDOW_TEXT_SIZE` (runner `.env`, default 14, range 10–28). The base is read in `controller_client/config.py` and passed to the window process at spawn. A−/A+ is session-only (Q2).
  - The default width is 640 (min 520), resizable; the height is capped at the screen minus `_SCREEN_MARGIN`, as today.
- **Performance**:
  - The log is one Text widget replaced only when the tail's hash changes, with a single insert (≤ 16 000 characters).
  - The chat is one Text widget with tags (author line, body, notice). New rows are appended by message id; it is fully re-rendered only when an earlier row changed or rows were dropped (≤ 40 rows).
  - The plan rows (≤ 30) and the test case are rendered once per change.
  - There are no per-message widgets, no `wraplength` labels for long text, and no `update_idletasks` loops.
- **IPC**:
  - `StateCommand` carries `cases`.
  - New window → runner events `PauseEvent`, `ChatSendEvent` and `HelpActionEvent`; a runner → window `CaseResultCommand`.
  - The manager forwards them like `ActionEvent` / `QuestionEvent`, with local validation, at most one pending of each kind, and a local timeout message.
- **Visibility** (manager):
  - The window wants to be visible when an unanswered step exists (today) **or** a help case is open that the person has not closed.
  - A running-only case never makes it show; it only enables Pause while the window is visible for another reason.
  - Every agent action still goes through `hidden_for_action`, including the help chat's `run_setup_task` actions, which run through the same `_dispatch`.
  - "News" for the 5 s re-show is a new step round, a new help case, or a turn that ended with a new assistant row. Anything else waits for the idle delay.
  - Sending a chat message or pausing never hides or raises the window (like Ask AI). A closed discussion stays hidden until something new arrives (a new help request id or a finished turn).
  - The window keeps its typed drafts across hides (same process).
  - `never takes focus`: today's show path is unchanged (no-activate on Windows; on macOS the known activation, with the front-app restore).
- **Pause…** opens an inline panel with an optional message ("What do you want to discuss?") and Pause / Cancel. It is enabled only when the case's `can_pause` is true, otherwise it shows `pause_blocker`; "Pausing…" shows while `pause_pending`.

### Settings / infrastructure
- Runner: `CONTROLLER_PERSON_WINDOW_TEXT_SIZE` (default 14) in `controller_client/config.py` and `controller_client/example.env`; the download `.env` (`projects/services.py`) gets it commented.
- Server: no new settings. Throttle constants live in the runner services, as today.
- Deploy: `migrate`, rebuild the server image, restart Daphne/web and the workers, then update runners from the Environment page.

### Data flow
1. A HITL step waits. The snapshot shows the step and its case; the window shows the Step tab, with Test case and Log one tab away.
2. The person presses Pause…, types "the dialog looks wrong, let's talk" and presses Pause. Window → `PauseEvent` → `case_pause_action` → `pause_from_runner` → `request_case_pause(actor, message)`. The waiting step converts at once into a paused help request; for an agent-running case it stops at the next check. On commit the broadcasts nudge the runner.
3. Once the request exists, the pause plan's `open_paused_help_chat` task sends the message as the first `case_help` turn. The `machine_setup` broadcasts nudge the runner, and the coalesced snapshot carries the help case and discussion with the turn live.
4. The agent may run setup tasks. The window hides for each action and re-shows when the turn ends with the reply.
5. The person replies (`person_chat_send` → `send_from_runner` → `send_setup_message`), sees the agent's plan redesign card, ticks "Use the redesigned plan" and presses Continue: `person_help_action` → `resolve_help_from_runner` → `continue_after_help(actor=...)`. The run wakes, and the next snapshot drops the help case.

## Alternatives considered
- **A web console**: rejected by the user (screenshots are runner-only, and a browser on the test machine would appear in captures).
- **The consumer joining `setup_group`** to relay chat events directly: rejected. It would put chat shaping in the consumer and leak every main-chat event to the runner. Nudges plus a bounded service snapshot keep one source of truth.
- **Separate `person_case_state` and `person_chat_state` messages**: more protocol surface, plus ordering issues between the step and case snapshots. One snapshot with deduplication and coalescing is simpler.
- **Requiring an identified member for chat and help actions from the runner**: safer for the audit trail, but against the user's rule ("the same actor rules as person_step_action, anonymous labelled as today"). Labels and `resolved_via` keep it visible.
- **Live log streaming to the window**: the window is hidden whenever the agent acts, so a live log would rarely be seen and would cost IPC on every line. A tail refreshed with snapshots is enough.

## Risks and mitigations
- **Anyone with the Connection key can chat with an agent that runs setup tasks on the machine, and can Continue / Mark failed / Mark blocked.** This is the same trust model the user accepted for person-step answers. Mitigations: an owner connection only, throttles, labels on every row ("via Test Runner"), `resolved_via` on the request, and identity counted only for active writable members.
- **Prompt injection through chat text**: unchanged. It is the same help-chat path as the web, and the person's text is a user turn.
- **Snapshot cost on chat bursts**: coalesced to at most 1 push per second per connection, about 5 queries only while a help case exists, and deduplicated.
- **Message size**: worst case about 300 KB per snapshot, well under the 4 MiB IPC cap and the server WebSocket limits. Every field is capped by constants shared by both parsers.
- **Actor refactor touching web help views and chat sends**: behaviour-neutral for web users, done in its own step, with every caller updated in that step and type-checked.
- **Pause plan dependency**: steps 1–5 do not need it. Step 6 wires `case_pause_action` to the pause plan's `pause_from_runner` once it has landed.
- **Tk on Windows and Linux** is untested on real machines (known from earlier notes), so smoke-test it on each OS available.

## Implementation steps
1. **Actor generalisation and migrations.** Files: `projects/models.py`, `projects/migrations/00xx_help_resolved_via.py`, `machine_setup/models.py`, `machine_setup/migrations/00xx_message_author_label.py`, `projects/case_help_services.py`, `projects/views.py` (help views), `machine_setup/services/send_message.py`, `open_help_chat.py`, `queue_turn.py`, `machine_setup/views.py`, `machine_setup/payloads.py`, `projects/person_step_actor.py` (docstring), and the help-history and help-card templates (label when no user).
2. **Protocol 0.11.0.** Files: `controller_client/protocol.py` (capabilities, payloads, caps, three messages and the result, parsers and serializers).
3. **Server read services.** Files: `projects/runner_case_services.py` (read part), `machine_setup/services/runner_discussion.py` (read part), `projects/runner_window_state.py`.
4. **Consumer, forms, nudges.** Files: `projects/controller_consumer.py`, `projects/forms.py`, `machine_setup/events.py`.
5. **Window v2.** Files: `controller_client/person_window_app.py` (Notebook, read-only blocks, fonts, tabs, one-tap report, chat and cards), `person_window_ipc.py`, `person_window.py` (visibility, forwarding), `client.py` (handlers for the new results and messages), `config.py`, `example.env`, and `person_window_diagnostics.py` (render a sample case and discussion).
6. **Write services and Pause wiring.**
   - Implement `resolve_help_from_runner` and `send_from_runner`.
   - Wire `case_pause_action` to the pause plan's `pause_from_runner`.
   - Let `open_paused_help_chat` send an anonymous runner pause message with the runner label.
   - Files: `projects/runner_case_services.py`, `machine_setup/services/runner_discussion.py`, `projects/runner_person_step_services.py` (owner and throttle helper shared with `runner_case_pause_services`), `projects/controller_consumer.py` (pause handler), and `machine_setup/services/open_paused_help_chat.py`.

Parallel sub-agents: after steps 1 and 2 are written (shared types), step 5 (controller) and steps 3–4 (server) touch disjoint files. Step 6 follows the pause plan.

## Verification
- `pre-commit run --all-files` (strict mypy for the server and `controller_client/`, including win32), `python manage.py check`, `makemigrations --check` after the two migrations, and `sqlmigrate` for both.
- Throwaway-DB shell:
  - count snapshot queries with no help case (1), and with a help case and a 40-message thread (≤ 5);
  - confirm `log_tail` never loads `logs` (inspect the SQL);
  - 20 quick nudges give 1–2 pushes.
- Cross-venv wire test (server → real `ControllerClient` → window stub): `cases` parsed and capped; the three new messages with the result; a 0.10.3 client gets no `cases` and its new messages are refused.
- Real Tk smoke (macOS, and Windows if available):
  - A−/A+ rescales everything; every block selects and copies;
  - a 16 000-character log and a 40-message chat render without lag (append-only updates);
  - Same as expected / Different… work;
  - Pause with a message → the discussion opens with that turn;
  - the window hides during the agent's setup task and re-shows after the reply;
  - Continue with the redesigned plan resumes the case;
  - an anonymous runner shows "a person at the Test Runner" on the web thread and in the help history.
- The web help card and help chat still work for web users after step 1 (Continue, Mark failed / blocked, open chat, send).
- `python -m controller_client.diagnose --window --no-interactive`.
- Run the `reviewer` and `verifier` agents.

## Open questions
- Q1. Should the window also show Need help cases of Attended / Night runs, not only HITL steps and pauses? (Decided for this plan: yes, whenever the request waits for a person and no Night auto-fix is live; a person at the machine is the point of the window. Confirm with the user if in doubt.)
- Q2. Should the A−/A+ choice be written back to the runner `.env`? (Assumption: no; session only, with the base from `CONTROLLER_PERSON_WINDOW_TEXT_SIZE`.)
- Q3. Should the wake gates be included in the help snapshot? They cost about 1–3 queries per push, and the earlier notes dropped them from step snapshots because they went stale. (Assumption: yes for help cases only, because Continue needs `can_continue`. A refused Continue still returns the reason.)

These are minor and do not block Ready. The Q1 decision follows the window's purpose and can be reversed with one filter.

## Changelog
- 2026-10-31: created, replacing the web console plan `feat-tester-console.md` after the user decided against a web UI.
- 2026-10-31: aligned with the revised `feat-pause-and-discuss.md` (Ready).
  - Pause calls its `pause_from_runner`; the own pause service and the 6/min throttle are dropped in favour of the shared 20/min runner bucket.
  - Adopted `case_pause_action` / `case_pause_action_result` / `CASE_PAUSE_V1`; this plan owns them and supersedes that plan's proposal.
  - The Continue radios use `allowed_continue_modes` (`continue_mode_choices`) and show the agent's suggested mode.
  - Anonymous pause messages are sent after step 6.
