# fix: Machine setup chat during a Manual run

Status: Done
Created: 2026-11-01
Request: While a run is Running, Machine setup refuses every message ("A test
run is in progress on this machine"). That is right while the agent drives the
machine, but not when no agent is using it: a run waiting for a person, or a
Manual run, where a person (not the agent) performs the cases. In those cases a
member must be able to chat with the Machine setup agent. The agent must still
never drive the machine while the tester is performing a claimed, unpaused
manual case, and the tester must not take the machine back while a setup turn
may be using it.

## Context

- Send gate: `machine_setup/services/send_gates.py::run_and_update_refusal`
  refuses a new turn while any run of the project is `STARTED`
  (`SetupRunActiveError`). Callers: `send_message._refusal` (via
  `thread_send_refusal` for main/help/result threads and
  `manual_thread_send_refusal` for the manual Discuss thread),
  `action_approval_decision._approve_locked`, and the Test Runner window
  (`runner_discussion.py`: `get_manual_discussion` with `own_manual_run_id`,
  `get_runner_send_blocker` via `thread_send_refusal`). The page mirror is
  `chat_page.py` (~558: `run_active` → `SetupRunActiveError().message` in
  `send_blockers`; `SetupPage.run_active` has no other reader).
- Only one run per project can be active (`projects/services.py::start_test_run`
  → `_has_active_test_run`, `ACTIVE_RUN_STATUSES`), and Start refuses while a
  setup turn is live (`live_turn_exists` → `MACHINE_SETUP_RUNNING_MESSAGE`).
- Waiting for a person is already PAUSED, not STARTED:
  `projects/services.py` (~4386) sets the run `PAUSED` when a HITL step goes
  `awaiting_person`; Need help and Pause-and-discuss also leave the run
  `PAUSED`. The remaining STARTED windows in an agent-driven run (planning,
  executing, confirming a person's report, a re-queued worker not yet picked
  up) all hand the machine to the agent shortly, so they keep refusing. Paused
  wake paths (help Continue, person step report, Continue run, Resume) already
  refuse while any setup turn is live (`run_reopen_services.list_wake_blockers`
  / reopen blockers → `live_turn_exists`). Confirmed: no other STARTED idle
  window in HITL; the request reduces to Manual runs.
- Manual runs (`RunHelpMode.MANUAL`, `docs/changed/2026-11-01-manual-run-mode.md`,
  `-manual-pause-and-discuss.md`, `-manual-run-resume.md`) stay `STARTED` for
  their whole life; no agent touches the machine. Today only the case's own
  `manual_case` Discuss thread is exempt (`own_manual_run_id`), and its
  `run_setup_task` runs only while the case is paused
  (`machine_setup/services/manual_chat_tools.py::manual_machine_refusal`,
  `MANUAL_DISCUSS_MACHINE_NEEDS_PAUSE`, gated in
  `setup_tools.build_setup_dispatcher._manual_case_gate`).
- The reverse direction today only looks at discussion threads
  (`machine_setup/turn_state.py`): claim refuses on `manual_run_turn_live`
  (`projects/manual_case_claim_services.py:93`), Continue on
  `manual_case_turn_live` (`projects/manual_case_pause_services.py:158`), the
  case page's Continue button on `manual_case_turn_live`
  (`projects/manual_case_display_services.py::_pause_panel`), release
  (`manual_case_claim_services.py:163`) and hand-in
  (`manual_case_submit_services.py:136`) on `manual_case_turn_live`. All lock
  the project row first (`manual_case_lock.lock_manual_run_case` →
  `lock_project_and_run`), as does `send_setup_message` (`resolve_live_turn`
  under the project lock), so a claim and a send are serialized.
- Manual Resume (`manual_run_resume_services.resume_manual_cases`) already
  refuses on reopen blockers, which include `live_turn_exists`; Start of a
  manual run already refuses on `live_turn_exists`. Neither needs a change.
- Machine readiness runner actions (`machine_readiness/services/runner_gates.py::runner_action_refusal`,
  used by `scan_artifacts` — also the main chat's `scan_artifacts` tool with
  `during_turn=True` — and `quick_check`) refuse while any run is `STARTED`
  (`RUN_STARTED_MESSAGE`). `machine_readiness` may import only
  `projects.models` / `decorators` / `exceptions`; runner gates come in as
  `RunnerPorts` from `projects/machine_readiness_ports.py`.
- No text in `run_catalog.py`, `run_state.py` or `setup_prompt.py` says setup is
  refused while a run is Started (checked); `run_status_label` just says
  "Running". No prompt change is required.
- Django ~=5.2.11; only plain ORM filters are used.

## Root cause

Confirmed: `run_and_update_refusal` treats every `STARTED` run as "the agent is
using the machine". That holds for agent-driven runs but not for Manual runs,
which are `STARTED` while a person tests (or nobody does: between cases, while
a case is paused). The page blocker in `chat_page.py` repeats the same test.
The safety the STARTED refusal gave for Manual runs (agent vs tester on the
machine) is only partly modelled today (discussion threads only), so lifting
the send gate needs the machine-level gates below.

## Goals
- A `STARTED` run in Manual mode does not refuse new setup turns: main chat,
  help/result threads, approval decisions, the manual Discuss thread, and the
  matching page/runner-window blockers.
- Agent-driven `STARTED` runs keep refusing exactly as today.
- No setup agent machine work (`run_setup_task`, `scan_artifacts`, quick
  check / scan from the readiness pages) while a claimed manual case of the
  started run is not paused.
- The tester cannot claim a case or Continue a paused case while any setup
  turn of the project is live.

## Non-goals
- Showing "agent busy" in the Test Runner snapshot ahead of time (the server
  refusal already reaches the window; unchanged runner).
- Gating claim/Continue on "a sub-task is actually running" instead of "a turn
  is live" (see Open questions Q2).
- Runner self-update during a manual run (`runner_update_offer.py` keeps its
  own STARTED refusal).
- Any change to HITL / Need help / Pause and discuss behaviour.
- Controller protocol, migration or settings changes.

## Design

### Data model
None.

### Services

New leaf module `projects/run_machine_use.py` (imports only `projects.models`;
importable by `machine_setup` and by `projects/machine_readiness_ports.py`; no
import of `projects.services`). It is the one definition of "a run is using
the machine":
- `agent_run_started(project_id: int) -> bool` — a `STARTED` run of the
  project whose `help_mode` is not `RunHelpMode.MANUAL` (one `exists()`).
- `manual_tester_on_machine(project_id: int) -> bool` — a `TestRunTestCase`
  of a `STARTED` Manual run of the project that is `IN_PROGRESS`, has a
  non-empty `manual_claim_token` and `manual_paused_at IS NULL` (one
  `exists()`; the same rule as `is_claimed_manual_case` + not paused, written
  as a filter). Docstring points at `manual_run_policy.is_claimed_manual_case`
  so both stay aligned.

`projects/manual_run_policy.py`: add `MANUAL_TESTER_ON_MACHINE` (user/agent
safe), e.g. "A tester is performing a manual case on this machine. Ask them to
pause it from the Manual test card or the Test Runner, then try again."

`machine_setup/services/send_gates.py`:
- `run_and_update_refusal(project, now)` refuses `SetupRunActiveError` only
  when `agent_run_started(project.pk)`. Drop the `own_manual_run_id`
  parameter: with one active run per project and Manual runs no longer
  refusing, it has no effect. Update its two callers
  (`manual_thread_send_refusal`, `runner_discussion.get_manual_discussion`)
  and the module docstring.
- `thread_send_refusal`, `manual_thread_send_refusal` otherwise unchanged
  (the manual thread keeps its "case still claimed" gate and no READY gate;
  help/result threads keep the READY gate).

`machine_setup/services/chat_page.py`: `run_active` = `agent_run_started(project.pk)`
(drop the inline `TestRun` query and, if now unused, the `TestRun` /
`TestRunStatus` imports). Field meaning becomes "an agent-driven run is
executing".

`machine_setup/services/setup_tools.py::build_setup_dispatcher._dispatch`:
for `RUN_SETUP_TASK_TOOL` in every role except `ThreadRole.MANUAL_CASE`
(which already has `manual_machine_refusal`, a stricter per-case check), call
`manual_tester_on_machine(project_id)` at call time and return
`_tool_error(tool_call, MANUAL_TESTER_ON_MACHINE)` when true. Order in the main
chat: intake gate, then this check, then the READY gate (keep
`_main_chat_gate` as the single place for main-chat gates; add a small
`_tester_gate` used by the other roles). Approved actions
(`run_setup_task(approval_id=)`) go through the same dispatcher, so approvals
are covered. Update the module docstring. `scan_artifacts` is covered by the
readiness gate below, not here.

`machine_readiness/runner_ports.py`: add a `ProjectCheckPort` Protocol
(`(project_id: int) -> bool`) and two fields `agent_run_started` and
`manual_tester_on_machine`. `projects/machine_readiness_ports.build_runner_ports`
wires them from `projects/run_machine_use.py`.

`machine_readiness/services/runner_gates.py::runner_action_refusal`: replace
the inline `STARTED` query with `ports.agent_run_started(project_id)` →
`RUN_STARTED_MESSAGE`, then `ports.manual_tester_on_machine(project_id)` → a new
`MANUAL_TESTER_MESSAGE` defined here (the module cannot import
`manual_run_policy`; same wording), then the existing turn / update /
connection gates. Query count stays "at most four"; update the docstrings
here and in `quick_check.py` (~148) / `scan_artifacts.py` (~251). The
`TestRun` / `TestRunStatus` imports go if unused.

Reverse gates (tester side), all already under the project row lock:
- `projects/manual_case_claim_services.claim_manual_case`: replace
  `manual_run_turn_live(test_run.pk)` with
  `live_turn_exists(project.pk, timezone.now())` → `MANUAL_PAUSE_AGENT_BUSY`
  (unchanged string, which the Test Runner window already mirrors as
  `_AGENT_BUSY`). Update the docstring.
- `projects/manual_case_pause_services.continue_manual_case`: replace
  `manual_case_turn_live(pivot.pk)` with `live_turn_exists(project.pk, now)`.
- `projects/manual_case_display_services._pause_panel`: same predicate for the
  disabled Continue button (`pivot.test_run.project_id`).
- Release and hand-in keep `manual_case_turn_live`: neither gives the machine
  back to the tester (a hand-in ends the claim; a new claim is gated), and the
  discussion-only check protects the Discuss thread going read-only.
- `machine_setup/turn_state.manual_run_turn_live` loses its last caller:
  delete it (update `live_turn_manual_case_ids`'s neighbours' docstrings if
  they mention it).

Why it is safe: a send and a claim/Continue both hold the project row lock, so
"turn live" and "case claimed and unpaused" cannot both start at once. While a
turn is live the tester cannot claim or Continue, so the state the
`run_setup_task` call-time check reads can only move towards "allowed"
(pause, hand-in, release). If the tester was already testing when the turn
started, every `run_setup_task` / scan is refused with a message the agent
relays ("ask the tester to pause").

### Pages and forms
No new pages. Behaviour changes:

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `machine_setup:chat`, `machine_setup:thread` | GET | `@project_membership_required` | — | `SetupPage.send_blockers` no longer lists `SetupRunActiveError` for a Manual run | — |
| main chat / thread send, approval approve | POST | writable member | existing | — | `SetupRunActiveError` only for an agent-driven started run |
| `projects:test_run_prepare_machine` | POST | writable member | — | redirects to chat | now accepted during a Manual run |
| readiness Scan / Check machine views | POST | writable member | existing | — | new `MANUAL_TESTER_MESSAGE` while a manual case is being tested; `RUN_STARTED_MESSAGE` only for agent runs |
| Manual test card Continue, Test Runner claim / continue | POST / runner message | existing | existing | Continue disabled while any setup turn is live | `MANUAL_PAUSE_AGENT_BUSY` while any setup turn is live |

### Background work and WebSockets
No new tasks, events or controller actions. `run_setup_turn` unchanged apart
from the dispatcher gate. The runner window gets the server's refusal text for
claim/Continue as today; no runner release needed.

### Settings / infrastructure
None.

### Data flow
1. Manual run Started, tester between cases or paused. A member sends in the
   main chat → `send_setup_message` → `run_and_update_refusal` sees no
   agent-driven started run → turn queued.
2. Agent calls `run_setup_task` → dispatcher → `manual_tester_on_machine`
   false → sub-agent runs. Meanwhile the tester's Claim / Continue →
   `live_turn_exists` → refused `MANUAL_PAUSE_AGENT_BUSY`; Pause still works.
3. Tester testing (claimed, unpaused) when a message is sent → turn queued;
   `run_setup_task` / `scan_artifacts` → refused with the tester message; the
   lead answers without touching the machine or asks for a pause.
4. Agent-driven run Started → unchanged refusal at send and on the page.

## Alternatives considered
- Exempt Manual runs only in the main chat (flag on `run_and_update_refusal`):
  more branches, and help/result threads would still be blocked during a
  Manual run for no machine reason; the call-time machine gate already
  protects the tester everywhere. Rejected for one rule.
- Refuse the send itself while a manual case is being tested: blocks pure
  chat (questions, KB, run proposals) for no reason; the call-time tool gate is
  enough and mirrors the Discuss thread's design.
- Duplicate the "agent run / tester on machine" queries in `runner_gates.py`:
  two definitions of the same rule; the existing `RunnerPorts` seam is cheap.
- A lock or lease on the machine shared by tester and agent: new state and a
  migration for a problem the project row lock already serializes.

## Risks and mitigations
- Tester blocked from claiming while a chat-only turn runs (up to the turn
  timeout). Mitigation: Stop / Force stop exist; Q2 offers a finer gate.
- Result chat on a case of the still-running Manual run can now propose a
  result change; Apply's own gates (`is_result_changeable`, push blockers) are
  unchanged and a handed-in manual case is already a settled verdict. Q1.
- A Test Runner window predating 0.16.0 has no Pause, so its tester can never
  free the machine mid-case; the agent's tools refuse with a clear message
  (fail-safe). No compatibility change.
- `SetupRunActiveError` text ("A test run is in progress on this machine")
  stays accurate for agent runs only, which is the only place it remains.
- Query cost: one extra `exists()` per `run_setup_task` call (non-manual
  roles) and per readiness action; no N+1.

## Implementation steps
1. Leaf predicate and message (files: `projects/run_machine_use.py` new,
   `projects/manual_run_policy.py`).
2. Setup send gates and page blocker (files:
   `machine_setup/services/send_gates.py`, `machine_setup/services/chat_page.py`,
   `machine_setup/services/runner_discussion.py`).
3. Call-time machine gate for `run_setup_task` (files:
   `machine_setup/services/setup_tools.py`).
4. Readiness runner gates (files: `machine_readiness/runner_ports.py`,
   `machine_readiness/services/runner_gates.py`,
   `projects/machine_readiness_ports.py`, docstrings in
   `machine_readiness/services/quick_check.py`, `scan_artifacts.py`).
5. Tester-side gates (files: `projects/manual_case_claim_services.py`,
   `projects/manual_case_pause_services.py`,
   `projects/manual_case_display_services.py`, `machine_setup/turn_state.py`
   — delete `manual_run_turn_live`).
6. `pre-commit run --all-files`; then CLAUDE.md Manual run paragraph ("Claim,
   Release, hand-in (retryable) and Continue are refused while a manual
   discussion turn is live") and the change note in step 6 of the workflow.

Steps 2–5 touch disjoint files and can run as parallel sub-agents after step 1.

## Verification
- `python manage.py check`, `mypy .`, `pre-commit run --all-files`.
- Manual (dev DB, a project with a `MANUAL_RUN_V1` runner and a ready visual
  engine):
  1. Start a Manual run, no case claimed: the Machine setup chat shows no
     "test run is in progress" blocker; send a message and get an answer;
     ask for a read-only machine check — the sub-task runs.
  2. During that turn, press Claim in the Test Runner window → refused "The
     agent is working on the machine…"; after the turn ends, Claim works.
  3. With the case claimed and not paused, ask the agent to check something
     on the machine → tool refusal with the tester message, no runner action
     in the runner log; `scan_artifacts` and the readiness Check machine /
     Scan buttons also refuse.
  4. Pause the case, repeat → sub-task runs; press Continue (web and window)
     during the turn → refused; Continue enabled after the turn.
  5. Manual Discuss thread still works as before (run_setup_task only while
     paused).
  6. Start an agent-driven (Attended) run: chat, approvals, readiness actions
     refuse exactly as before.
  7. Approve an approval card during a Manual run with a case being tested →
     the turn is queued and `run_setup_task(approval_id=)` refuses with the
     tester message (the card is not consumed as an attempt; verify the
     approval stays retryable, see Q3).
- Run `reviewer` and `verifier`.

## Open questions
- Q1: Should help and result threads (and their approvals) follow the same
  rule, i.e. be sendable during a Manual run? (assumption used in this draft:
  yes, one rule for every thread; their `run_setup_task` is gated by the same
  call-time tester check and help/result keep the READY gate.)
- Q2: Is "any setup turn live" the right lock for Claim / Continue, even when
  the turn is only chatting and never touches the machine? (assumption: yes,
  simplest and race-free; a finer "a setup sub-task is running" gate would
  need a new live-sub-task query and a call-time re-check.)
- Q3: When an approved action's `run_setup_task` is refused because the tester
  is testing, should that count toward the card's two attempts? (assumption:
  no — attempts are spent inside `machine_setup/services/run_setup_task.py`
  (reached through `run_subtask`), and the new refusal returns from the
  dispatcher before `run_subtask`, so no attempt is spent; keep it there.)
- Q4: Should Check machine / Scan on the readiness pages also be allowed
  during a Manual run when no case is being tested? (assumption: yes, via the
  same ports, so the main chat's `scan_artifacts` and the buttons agree with
  `run_setup_task`.)

## Changelog
- 2026-11-01: created
- 2026-11-01: open questions answered with the defaults (user); implemented.
- 2026-11-01: review fixes — `claimed_manual_run_id` → `is_claimed_manual_pivot`
  (bool); stale comments reworded; the runner window's Continue is now also
  disabled while any setup turn is live (`get_runner_manual_discussion`
  returns `RunnerManualDiscussion` with `setup_turn_live`, no extra query, no
  protocol change), so web and window agree.
- 2026-11-01: rejected — main-chat gate order intake → READY → tester (not
  intake → tester → READY): only the first refusal message differs; keeps
  `_main_chat_gate` unchanged.
- 2026-11-01: accepted — a claim does not wait for a queued or running
  readiness scan / quick check (cache locks, not the project lock). Both only
  stat files, and agent-run Start has the same gap today.
