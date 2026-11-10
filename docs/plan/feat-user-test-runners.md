# feat: User-owned Test Runners, assigned per run

Status: Done (Phase 1; Phase 2 deferred)
Created: 2026-11-09
Request: Today each project has exactly one Test Runner (controller client). It
authenticates with the project's Connection key, and one runner socket is active per
project. The user wants runners to belong to **users**. A user can have many runners
connected at once, each with a unique human-assigned name, and picks which runner
executes a run when starting it. That way many runs, of the same or different
projects, run in parallel on different runners. There is **no backward
compatibility**: every installed runner is replaced by a fresh install that
authenticates with its own runner token. The project Connection key stays only as
the MCP credential.

## Context

### What exists today (the "one runner per project" model)

- **Auth and connection.**
  - The runner handshake (`ws/controller/`, `auto_tester/asgi.py` →
    `projects/controller_consumer.ControllerConsumer`) sends `api_key` =
    `Project.api_key`.
  - `projects/controller_authenticator.py` resolves it with
    `projects.services.get_project_by_api_key` and checks
    `REQUIRED_CLIENT_CAPABILITIES`.
  - `claim_agent_connection` writes the connection onto the **Project row**:
    `agent_connected`, `agent_channel_name`, `agent_system_info`,
    `agent_client_version`, `agent_capabilities`, `agent_device_*`,
    `last_connected_at`.
  - The newest socket wins; the old one is closed with `CloseCode.SUPERSEDED`
    (4001).
- **Dispatch.**
  - Every `controller_*` function in `projects/services.py` takes `project_id`.
    It sends to the channel group `controller_{project_id}` and waits for the
    connection owner with `projects/controller_reconnect_services.wait_for_controller(project_id)`.
  - Callers outside `projects/services.py`:
    - `agents/services/tools_controller.py` (34 calls), `tools_mobile.py` (8),
      `verified_element_actions.py`, `controller_element_finder.py`,
      `browser_element_finder.py`
    - `projects/controller_{mobile,capture,web_browser}_actions.py`,
      `controller_command_sessions.py`, `machine_readiness_ports.py`,
      `capture_phone_apps_services.py`
    - `mcp_gateway/services/{browse_actions,capture_screenshot,command_sessions,list_windows,machine_screenshot,stop_claim_commands}.py`
    - the `test_controller_*` management commands
  - The agent finds its runner through `agents.types.ToolContext.project_id`.
- **Machine state** comes from `projects.services.MachineState` /
  `get_machine_state(project)` plus `projects/runner_target.py` and the global
  visual engine. Its readers:
  - run readiness checks
  - `machine_setup` (`send_gates`, `setup_tools`, `chat_page`, `case_auto_fix`,
    `run_setup_task`, `readiness_tools`, `run_turn`, `consumers`)
  - `machine_readiness` (`runner_info`, `runner_ports`, `run_stage0_state`,
    `views`)
  - `mcp_gateway/services/{machine_gate,read_project}.py`
  - `dashboard/services/all_projects.py`, `projects/views.py`,
    `projects/templatetags/runner_device.py`
  - templates `projects/environment.html`, `overview.html`,
    `includes/_project_table.html`, `wizard/step_machine.html`,
    `machine_setup/chat.html`
  - `static/js/punk-hazard.js`
- **Status push** uses the `agent_status_{project_id}` group: `AgentStatusConsumer`,
  `machine_setup/consumers.py`, `projects/tasks.py`, `runner_update_offer`.
- **Exclusivity is per project.**
  - `ACTIVE_RUN_STATUSES` ("holds the project's machine"); `start_test_run` locks
    the Project row and refuses a second active run.
  - `projects/run_machine_use.py`.
  - `machine_setup.turn_state.live_turn_exists(project_id)` and the constraint
    `setup_one_live_turn_per_project`.
  - `RunnerUpdate` (project FK, `runner_update_one_in_flight`).
  - `machine_readiness/project_locks.py` scan and quick-check locks.
  - MCP `mcp:machine-lock:{project_id}`.
  - `capture_phone_apps:load:{project_id}`.
  - Runner answer throttle `projects:runner-person-answer:{project_id}`.
- **Per-project runner features.**
  - Person window: `runner_window_state`, `runner_person_step_services`
    (`owns_project`, `resolve_runner_person` from `CONTROLLER_PERSON_EMAIL`),
    `runner_case_pause_services`, `runner_help_action_services`,
    `runner_manual_services`, `runner_manual_state_services`. Nudged through
    `controller_{project_id}` by `runner_person_step_push`, called from
    `projects/services.py`, `machine_setup/events.py`,
    `manual_screenshot_services.py` and `run_case_retest_services.py`.
  - Manual claims (`manual_case_claim_services`: "the project's running manual
    run").
  - Self-update (`RunnerUpdate`, `runner_update_{request,offer,progress}`,
    `runner_bundle.py`).
  - Phone runner (`runner_target.py`, `runner_device_status_services.py`).
  - Capability checks (`runner_capability_services.py`,
    `browser_selection_services.py`).
  - Readiness (`ArtifactInventory` unique per project and OS;
    `Stage0Requirement.checked_host`).
  - The project ZIP (`services._generate_env_content`,
    `views.download_controller_client`), the Environment page and its Connection
    key card, and the wizard step `setup_machine`.
- **The Connection key is also the MCP credential.**
  `mcp_gateway/services/authenticate_bearer.py` uses `get_project_by_api_key`, and
  `McpGrantProject.api_key_fingerprint` ties OAuth grants to it.
- **Execution.** `start_test_run` queues one Celery `chain` per run on `execution`
  (`docker-compose.yml`: `--concurrency=10`, prefork, prefetch 1, acks late). Runs of
  different projects already execute in parallel; only the per-project gate
  serializes them.
- **Protocol** (`controller_client/protocol.py`).
  - `CLIENT_VERSION` 0.24.0.
  - Handshake fields: `api_key`, `client_version`, `capabilities`,
    `update_in_flight_id`, `person_email`.
  - `handshake_ack` carries `project_id` / `project_name`.
  - The client treats an `error` or `incompatible` ack as terminal: it raises
    `AuthenticationError` and stops reconnecting (`controller_client/client.py`
    `_TERMINAL_HANDSHAKE_STATUSES`). An old runner refused at handshake therefore
    exits instead of hammering the server.
- **Resume** accepts DONE and CANCELLED runs (`run_reopen_services.REOPENABLE_RUN_STATUSES`)
  and keeps case checkpoints, which the deploy-time migration relies on.

### Inventory of "one runner per project" assumptions

Every row is re-keyed to the runner in this change, or explicitly kept per project.

| # | Where | Today | New rule | Phase |
|---|---|---|---|---|
| A1 | `Project.agent_*`, `last_connected_at` | connection on project | `TestRunner` row; project columns dropped | 1 |
| A2 | `Project.api_key` + `get_project_by_api_key` in the handshake | project key authenticates runner | runner token only; key = MCP only | 1 |
| A3 | `controller_{project_id}` (`services._controller_group`, `controller_consumer._controller_group_name`, `runner_person_step_push`) | group per project | `runner_{runner_id}` | 1 |
| A4 | `claim_agent_connection`, `release_agent_connection`, `force_disconnect_controller`, `_clear_agent_connection` | supersede per project | per runner (`runners/services/runner_connection.py`) | 1 |
| A5 | `wait_for_controller`, `connected_owner`, `controller_connection_lost`, `_has_started_run(project_id)` | waits while project has STARTED run | per runner, while it holds a STARTED run | 1 |
| A6 | every `controller_*(project_id, …)`, `dispatch_controller_action`, `ToolContext.project_id` | project selects runner | `RunnerId` selects runner | 1 |
| A7 | `MachineState`, `get_machine_state`, `connected_machine_state`, `runner_target.*`, `device_offline` | per project | per runner | 1 |
| A8 | `agent_status_{project_id}`, `AgentStatusConsumer`, `broadcast_agent_status`, `runner_update_offer.broadcast_runner_update` | per project | `runner_status_{id}` + `runner_owner_{user_id}`; `AgentStatusConsumer` removed | 1 |
| A9 | `_has_active_test_run`, `_no_active_run_check`, `start_test_run`, `run_reopen_services`, `run_resume_services`, `rerun_cases`, `ACTIVE_RUN_STATUSES` comment | one active run per project | Phase 1: runner held + project rule kept; Phase 2: runner only | 1 / 2 |
| A10 | `run_machine_use.agent_run_started/manual_tester_on_machine(project_id)` | per project | `projects/runner_use.py` per runner | 1 |
| A11 | `live_turn_exists(project_id)`, `setup_one_live_turn_per_project` | one setup turn per project | Phase 1: kept + runner hold; Phase 2: one per runner and one per conversation | 1 / 2 |
| A12 | `RunnerUpdate.project`, `runner_update_one_in_flight`, `runner_update_in_progress`, `expire_stale_runner_updates` | per project | per runner | 1 |
| A13 | `machine_readiness.project_locks` SCAN / QUICK_CHECK | per project | per runner (ANALYSIS stays per project) | 1 |
| A14 | `ArtifactInventory` unique (project, os_name) | one machine | (project, runner, os_name) | 1 |
| A15 | `Stage0Requirement.checked_host` | host rule | kept unchanged | – |
| A16 | `mcp:machine-lock:{project_id}`, `machine_gate._ready_project`, `read_project` | project runner | runner of the claimed case's run | 1 |
| A17 | `capture_phone_apps:load:{project_id}`, `capture_phone_apps_services` | project runner | a runner chosen on the card | 1 |
| A18 | `runner_window_state`, `owns_project`, `runner_manual_services._check_owner_and_throttle`, `runner_case_pause_services`, `runner_help_action_services`, throttle | project's steps; project socket | runs assigned to the runner; runner socket; per-runner throttle | 1 |
| A19 | `resolve_runner_person(project_id, person_email)`, `CONTROLLER_PERSON_EMAIL` | identity by email | identity = runner owner; email removed | 1 |
| A20 | `manual_case_claim_services` "the project's running manual run", manual upload owner check | one manual run per project at the window | runs assigned to the runner; uploads only from the hand-in's runner | 1 (claim lookup by run: 2) |
| A21 | `abort_active_test_run_on_disconnect(project)` | aborts project's run | aborts the runs held by the runner | 1 |
| A22 | `_generate_env_content`, `generate_controller_client_zip`, `download_controller_client`, `regenerate_api_key` wording, wizard `setup_machine`, Environment page | ZIP per project | ZIP per runner on `/runners/`; project download removed | 1 |
| A23 | `dashboard/services/all_projects.py`, `_project_table.html`, `overview.html` | project machine badge | active run count + runner of the latest active run | 1 |
| A24 | `projects/admin.py` | runner fields on Project | `TestRunner` admin | 1 |
| A25 | `runner_capability_services` (`runner_supports`, `runner_os_name`), `browser_selection_services` | project runner | runner | 1 |
| A26 | `person_step_services` shell label, `agents/os_shell.py` callers | project runner OS | run's runner OS | 1 |
| A27 | `machine_setup` threads, `run_setup_task`, READY gate | "the" machine | thread's runner | 1 |
| A28 | `projects/tasks.py` (status group, `scan_artifacts_task`, `quick_check_task`) | project id | runner id in args | 1 |
| A29 | MCP OAuth key fingerprint | key change kills grants | unchanged (key stays for MCP) | – |
| A30 | single-active-run lookups: `manual_case_claim_services`, `case_pause_services`, `person_step_services`, `capture_phone_apps_services`, `mcp_gateway/services/read_runs`, `testrail_case_refresh_services`, `run_triage/services/card` | at most one active run per project | audited for many active runs | 2 |

## Decisions

Answers from the user (the recommended answers, accepted). Q5 is replaced by "no
backward compatibility".

- **Q1 Name uniqueness:** unique per owner, case-insensitive. Wherever another
  user's runner appears (run card, hold labels), it is shown as "name (owner)".
- **Q2 Sharing:** only the owner may assign their runner. There is no sharing (a
  later plan).
- **Q3 Acting through a run:** once the owner assigns a run to their runner, any
  writable member of that project may act on that run while it holds the runner:
  - Resume and help Continue
  - help and result chats with `run_setup_task`
  - person-step answers on the web

  The owner can Release the runner or reassign the run at any time; reassigning
  to a runner requires owning it.
- **Q4 Connection key:** stays as the MCP credential only, so OAuth grant
  fingerprints keep working. It no longer authenticates runners. The UI calls it
  "Connection key (MCP)".
- **Q5 (replaced) Transition:** no backward compatibility.
  - Runners below the new `CLIENT_VERSION` (0.25.0) are refused at handshake, and
    users install fresh runners from `/runners/`.
  - There are no legacy rows, no rebind message and no adoption flow.
- **Q6 Switching runner:** allowed only while the run is not executing (WAITING,
  PAUSED, or DONE/CANCELLED before Resume or Re-run). A switch withholds the
  `current_screen` continue mode. A Started run cannot switch; pause it first.
- **Q7 Token re-download:** the token is stored Fernet-encrypted plus a sha256
  hash, so the owner can download the ZIP again without minting a new token.
- **Q8 Superusers:** cannot assign other users' runners. They see every runner on
  `/runners/all/` (read-only) and may Force disconnect.
- **Q9 Name visibility:** the runner name appears in MCP replies only as "test
  machine <name>", and never in TestRail.
- **Q10 Capacity:** no cap on parallel runs. The `execution` worker concurrency is
  documented as the deployment's limit on parallel agent runs.
- **Q11 Setup parallelism:** one live setup turn per runner and one per
  conversation (Phase 2), so turns of one project may run on different runners at
  once.
- **Q12 Manual runs:** the runner is optional.
  - Without one, MCP `add_screenshot` works and the machine and capture tools
    answer `[not_set_up]`.
  - With one, the runner window shows the run, and the runner is held only while
    a case is claimed and not paused.
- **Q13 Default on Start:** preselect the user's runner last used for this project
  if it is free; else the only free Ready runner of the user; else none.

## Goals

- A **Test Runner** belongs to one user. It has a human-given name (unique per
  owner), its own secret token and its own connection state, system info,
  capabilities, device state, self-update and person window.
- A user may have many runners connected at once.
- Starting a run requires choosing a runner the starter owns. Resume, Re-run, help
  Continue and "Change runner" keep or reassign it under the Q6 rule.
- A runner serves at most **one holder** at a time:
  - an active agent run (STARTED or PAUSED)
  - a claimed, unpaused Manual case
  - a live Machine setup turn
  - a readiness scan or quick check
  - an MCP claim's machine tools
  - a self-update
- After Phase 2, runs of the same project run in parallel on different runners. In
  Phase 1 this already works across projects.
- Every project feature that uses "the machine" names its runner explicitly.
- Runs, cases, logs, screenshots, help requests, checkpoints and history survive
  the migration.

## Non-goals

- Backward compatibility with runners at or below 0.24.x, or with Connection-key
  runner auth.
- Pooling or automatic scheduling; sharing runners between users.
- Splitting one run's cases across runners; switching a Started run.
- Per-runner Stage0 requirement statuses (the host rule stays).
- Changes to the vision worker, the LLM clients, notifications or the Night digest.

## Design

### Data model

New app **`runners`** (one domain: the Test Runner).

- `runners.TestRunner`:
  - `owner` FK → user, `PROTECT`
  - `name` CharField(64), plain text, always escaped. Unique per owner
    case-insensitively: `UniqueConstraint(Lower("name"), "owner")`.
  - `token_hash`: sha256 hex, unique. Lookup is an indexed equality, then a
    constant-time compare.
  - `token_encrypted`: Fernet, via `projects/secrets.encrypt_secret`.
  - Connection state (moved from Project, new names): `connected` (indexed),
    `channel_name`, `system_info`, `client_version`, `capabilities`,
    `device_state`, `device_detail`, `device_reported_at`, `last_connected_at`.
  - `created_at`, `updated_at`, `archived_at` (soft delete, so finished runs keep
    a name).
- `projects.TestRun`:
  - `runner` FK → `runners.TestRunner`, nullable, `SET_NULL`
  - `runner_name_snapshot` CharField(64)
  - `runner_assigned_at` DateTime, null

  Null means no runner. That is allowed for WAITING runs, Manual runs, and runs
  carried over from before the deploy.
- `projects.RunnerUpdate`: `project` FK → `runner` FK (non-null). The in-flight
  constraint and index are re-keyed to `runner`.
- `projects.ManualCaseSubmission.runner` FK, nullable: the runner that handed in.
  Spool uploads are accepted only from it.
- `projects.Project`: drop `agent_connected`, `agent_channel_name`,
  `agent_system_info`, `agent_client_version`, `agent_capabilities`,
  `agent_device_state`, `agent_device_detail`, `agent_device_reported_at`,
  `last_connected_at`. Keep `api_key` (MCP).
- `machine_setup.SetupTurn.runner` FK, nullable. Null means a turn that needs no
  machine (triage), or a main-chat turn with no runner selected.
- `machine_setup.SetupConversation.runner` FK, nullable: the main chat's selected
  runner. Other kinds use their run's runner.
- `machine_readiness.ArtifactInventory.runner` FK, nullable. Rows from before the
  deploy keep null and are shown as "Scanned by an earlier Test Runner (<host>)"
  until a runner scans that OS. Constraints:
  - unique (project, runner, os_name) where runner is set
  - unique (project, os_name) where runner is null
- Strongly typed id: `runners/types.py` `RunnerId = NewType("RunnerId", int)`.
  `controller_*`, `wait_for_controller`, `ToolContext.runner_id` and every
  runner-keyed service take `RunnerId`. Mypy strict rejects a plain `int` (such as
  a project id) at each call site, which is how every A6 site is found.

### Services

`runners/services/` (one use case per module):

- `create_runner.py`: owner and name → row plus the plaintext token, returned
  once. Enforces `RUNNER_MAX_PER_USER`.
- `rename_runner.py`.
- `archive_runner.py`: refused while the runner is held or assigned to an active
  run; force-disconnects.
- `reissue_token.py`: a new token; the live socket is closed with 4002.
- `runner_zip.py`: builds the `.env` and calls
  `projects.runner_bundle.build_runner_archive`. It replaces
  `projects.services._generate_env_content` and `generate_controller_client_zip`.
  The `.env` holds `CONTROLLER_HOST`, `CONTROLLER_PORT`, `CONTROLLER_RUNNER_TOKEN`,
  `CONTROLLER_RUNNER_NAME` (display only), the proxy and reconnect lines,
  `CONTROLLER_PERSON_WINDOW` and the text-size and spool lines. There is no
  person email.
- `authenticate_runner.py`: token → runner. Refused for an archived runner or an
  inactive owner.
- `runner_connection.py`: `claim_runner_connection` and
  `release_runner_connection` (supersede per runner), and `force_disconnect_runner`
  (aborts the runs held by the runner, A21).
- `runner_access.py`, the one predicate set:
  - `usable_runners(user, project)`: the user's non-archived runners, if the user
    is a writable member.
  - `can_assign(user, runner, project)`
  - `can_manage(user, runner)` (owner)
  - `can_view(user, runner)`: the owner, a superuser, or a member of a project
    whose active run holds it.
- `runner_status.py`:
  - `runner_machine_state(runner, visual_engine)`: the per-runner `MachineState`,
    which moves here from `projects.services`. The device rule moves from
    `runner_target.py`.
  - broadcasts to `runner_status_{id}` and `runner_owner_{user_id}`.

`projects` (dispatch and assignment stay next to the runs):

- **Controller dispatch.** The controller section of `projects/services.py` and
  `controller_reconnect_services` are keyed by `RunnerId`, using the group
  `runner_{id}`.
- **`projects/runner_use.py`** (new leaf, replaces `run_machine_use.py`) is the one
  definition of "runner held". It reads:
  - active agent runs with `runner_id`
  - claimed unpaused Manual cases of runs with `runner_id`
  - live setup turns with `runner_id`, through `machine_setup.turn_state` (a leaf)
  - the in-flight `RunnerUpdate`
  - scan and quick-check locks for the runner

  It returns a `RunnerHold` reason (for messages), or None. Every acquiring
  service takes the **runner row lock** (`select_for_update`) and then checks
  `runner_use`. The lock order is runner, then project (while the Phase 1
  project rule lives), then run. A reassign locks both runners in id order.
- **`projects/run_runner_assignment.py`** (new):
  - `assign_runner`: WAITING runs, Start, and the new run of a Re-run.
  - `reassign_runner`: PAUSED, DONE or CANCELLED runs.

  Both check `can_assign`, and that the runner is connected, READY and not held
  by another holder. They write `runner`, `runner_name_snapshot` and
  `runner_assigned_at`, and nudge the old and new runner windows.
  `start_test_run`, `resume_test_run(_case)`, `rerun_cases`, help Continue,
  person-step reports and the Manual claim all require "the run's runner is set,
  connected, compatible and not held by someone else". When the run has no runner,
  the page asks for one.
- **Continue mode.** `case_pause_policy.allowed_continue_modes` withholds
  `current_screen` when the run's runner is null or `runner_assigned_at` is later
  than the help request's `created_at`. A waiting HITL person step is handed back
  on reassign, as on "switch away" today.
- **Runner window.** `runner_window_state` and the `runner_*_services` take
  `runner_id`.
  - The window shows the open person steps, help cases, running case and Manual
    runs of **runs assigned to this runner**, across every project where the owner
    is a writable member.
  - Answers are checked with `owns_runner(runner_id, channel)` and
    `run.runner_id == runner_id`, and throttled per runner.
  - The actor is "<owner> via Test Runner" while the owner is a writable member of
    the run's project; otherwise the answer is refused.
  - `resolve_runner_person` and the `person_email` handling are removed.

`machine_setup`:

- **Runner per thread.** Help, auto-fix, result and manual threads use their run's
  current runner. The main chat uses `SetupConversation.runner`, picked in the chat
  header from `usable_runners`. Triage uses none.
- **Turns.** A turn records its runner when queued. `run_setup_task` and the READY
  gate use it. Tools refuse when the thread's runner changed since then. With no
  runner, the machine tools say "No Test Runner selected", as the main chat says
  "not connected" today.
- **Send gates.** A started agent run on the same runner refuses a new turn. In
  Phase 1 the per-project live-turn rule also stays.

`machine_readiness`:

- `RunnerPorts` take a `runner_id`.
- Scan and quick check need a runner: the run's runner on the run card, a picker
  of `usable_runners` on the Machine readiness tab.
- Locks are keyed by runner, and the inventory per (project, runner).

`mcp_gateway`:

- **Machine tools and capture.** They use the runner of the claimed case's run.
  `machine_gate` reads it in its one query through the pivot. No runner gives
  `[not_set_up]`; disconnected gives `[machine_offline]`.
- **Machine lock.** `machine_round_trip` locks `mcp:machine-lock:runner:{id}`, and
  the claim is the `runner_use` "claimed unpaused Manual case" hold.
- **`read_project`.** Reports the machine of the project's active Manual run's
  runner.
- **Unchanged.** The Connection key and the no-trace rule do not change.

### Pages and forms

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `/runners/` (`runners:list`) | GET | login | – | `runners/list.html`: `runners` (state, OS/device, version, hold label + link when the viewer can see the project), `add_form`, `max_runners` | – |
| `/runners/add/` | POST | login | `RunnerCreateForm(name)` | redirect to detail; token shown once (session flash, never in the URL) with Download | name taken, limit reached |
| `/runners/<id>/` | GET | owner | – | `runners/detail.html`: state, system info, capabilities, update offer, hold, assigned active runs, install steps | 404 otherwise |
| `/runners/<id>/rename/` | POST | owner | `RunnerRenameForm` | redirect | duplicate name |
| `/runners/<id>/download/` | POST | owner | – | ZIP `test-runner-<slug>.zip` | archived → 404 |
| `/runners/<id>/token/` | POST | owner | confirm | redirect, new token shown once | – |
| `/runners/<id>/update/` | POST | owner | `RunnerUpdateForm` (moved from projects) | redirect | held, no self-update |
| `/runners/<id>/release/` | POST | owner | confirm | redirect; ends the runner's hold: an agent run is paused with the `runner_disconnected` fault, a Manual claim is released | – |
| `/runners/<id>/disconnect/` | POST | owner or superuser | – | redirect | – |
| `/runners/<id>/archive/` | POST | owner | confirm | redirect | held, or assigned to an active run |
| `/runners/all/` | GET | superuser | – | read-only list of every runner | – |
| `projects:test_run_start` | POST | `@project_membership_required` (writable) | `TestRunStartForm` + `runner` (choices = `usable_runners`, initial per Q13; Manual: optional) | "Ready to run?" shows the chosen runner's checks | not allowed, offline, held, device not ready |
| `projects:test_run_runner` (new) | POST | writable | `RunRunnerForm(runner)` | run page "Test Runner" card: runner, badge (WS), Change | run Started; not owner of target |
| Resume / Re-run / case Resume / help Continue / person-step report | POST | writable | existing forms + optional `runner` (default = the run's runner; required when null) | – | as above + Q6 continue rule |
| `machine_setup:chat` | GET/POST | writable | `SetupRunnerSelectForm` in the header | `thread_runner`, `runner_choices`, `runner_state` | – |
| `projects:environment` (tab renamed "Test Runners") | GET | member | – | runners held by this project's active runs (name (owner), badge), a link to `/runners/`, the Connection key (MCP) card with the "MCP clients only" wording | – |
| `projects:regenerate_api_key` | POST | writable | existing | dialog wording: affects MCP clients only | – |
| `projects:download_controller_client` | – | – | **removed** | – | – |
| wizard `setup_machine` | GET/POST | writable | inline `RunnerCreateForm` | `runner_choices` (own runners with state), add form, download after add; reads real state only (no wizard state) | – |
| wizard `setup_run` | POST | writable | start form with `runner` | – | as Start |

Read-only members see the runner name and badge on run pages only. Runner names,
hostnames and system info are always escaped; they are never passed through
`|safe`.

### Background work and WebSockets

- **Controller socket** (`ws/controller/`).
  - The handshake requires `runner_token`, and `REQUIRED_CLIENT_CAPABILITIES`
    gains `USER_RUNNER_V1`.
  - A handshake without a token, or without the capability, gets
    `status="incompatible"` with the message "This Test Runner is outdated. Install
    a new one from <SITE_URL>/runners/". An old runner treats this as terminal and
    exits.
  - `api_key` and `person_email` are no longer read.
  - The consumer keeps `self._runner_id` and joins `runner_{id}`.
  - `handshake_ack` returns `runner_id` and `runner_name` instead of `project_*`.
- **Client (0.25.0).**
  - `controller_client/config.py` reads `CONTROLLER_RUNNER_TOKEN`
    (`--runner-token`). `CONTROLLER_API_KEY`, `--api-key`,
    `CONTROLLER_PERSON_EMAIL`, `--person-email` and the first-start email prompt
    are removed.
  - The window title shows the runner name.
  - Self-update still copies `.env`, so the token survives updates.
- **Status groups.**
  - `runner_status_{id}` feeds the run page card, the setup chat header and the
    runner detail page.
  - `runner_owner_{user_id}` feeds the `/runners/` list.
  - New `runners/consumers.RunnerStatusConsumer` at `ws/runners/<id>/status/`
    (`can_view`) and `ws/runners/status/` (own list).
  - `AgentStatusConsumer` and the `agent-status` route are removed.
  - `machine_setup/consumers.py` subscribes to its thread runner's group.
  - The visual engine group stays global.
- **Person window nudges** go to `runner_{runner_id}`
  (`nudge_runner_person_steps(runner_id)`); callers pass the run's runner. A
  reassign nudges both runners.
- **Celery.**
  - Chains are unchanged: one per run, and parallel runs are parallel chains.
  - The case task reads `run.runner_id` at start and builds
    `ToolContext(project_id, runner_id)`.
  - A reassign bumps `execution_generation`, so stale tasks stand down.
  - Setup turns, scans and quick checks carry `runner_id` in their args and
    re-check under the runner lock.
- **Capacity.** One case holds one `execution` worker for its whole duration, so
  concurrency = the maximum number of parallel agent runs (Q10). `assist` (4)
  bounds parallel setup turns and scans. Document both in `docker-compose.yml`
  and `CLAUDE.md`.

### Settings / infrastructure

- `RUNNER_MAX_PER_USER` (default 20): read in `settings.py` via decouple and added
  to `example.env`.
- Runner `.env`: update `controller_client/example.env` and the setup scripts'
  docs for `CONTROLLER_RUNNER_TOKEN` / `CONTROLLER_RUNNER_NAME`.
- Wiring: `auto_tester/asgi.py` adds the runners routing and drops `agent-status`;
  `auto_tester/urls.py` adds `runners/`.
- `scripts/verify_server_image.sh` must still pass (the bundle path is unchanged).

### Migration and deploy (runs active at deploy time)

Migrations, in dependency order:

1. `runners.0001`: create `TestRunner`. It starts empty, because no project
   runner can be converted (no owner, no token).
2. `projects` (additive): `TestRun.runner`, `runner_name_snapshot`,
   `runner_assigned_at`, `ManualCaseSubmission.runner`, `RunnerUpdate.runner`
   (nullable for now).
3. `projects` RunPython, written against historical models with plain updates:
   - **STARTED agent runs** (not Manual) → CANCELLED, with `finished_at = now` and
     `execution_generation + 1`, so any queued chain task stands down. Their
     IN_PROGRESS cases → CANCELLED. Checkpoints are kept, so a person Resumes later
     on a chosen runner (Resume already accepts CANCELLED runs).
   - **PAUSED runs** (Need help or Your turn) stay PAUSED with no runner. The help
     request, person step and checkpoint are kept. Continue or answer asks for a
     runner first, and `current_screen` is withheld because the runner is null.
   - **STARTED Manual runs** stay STARTED with no runner (Q12).
     - Claims made at the old runner window (`manual_claim_source` ≠ MCP) are
       released: the case goes back to Created and the claim fields are cleared,
       as `release_manual_case` does.
     - MCP claims are kept; their machine tools answer `[not_set_up]` until a
       runner is assigned.
     - Hand-ins already stored but still uploading (`ManualUploadState`
       uploading) are marked incomplete. The old runner's spool can no longer
       upload them.
   - **RunnerUpdate** rows are deleted. They only describe the retired project
     runners and are referenced nowhere else.
4. `projects`: `RunnerUpdate.runner` becomes non-null; drop `RunnerUpdate.project`
   and its constraint and index; add the runner constraint and index; drop the
   `Project.agent_*` and `last_connected_at` columns.
5. `machine_setup`: add `SetupTurn.runner` and `SetupConversation.runner`. A
   RunPython marks every live turn (queued or running) as ended, using the same
   end reason Force stop writes. Workers restart on deploy anyway.
6. `machine_readiness`: add `ArtifactInventory.runner` and swap the unique
   constraints. Existing rows keep null (no data loss).

Reverse migrations restore the columns with defaults. The cancelled runs, released
claims and deleted update rows are not restored (noted in the change note).
Readiness and MCP lock keys change names, and the old keys expire on their TTLs.

Deploy checklist (written into the change note):
- Announce the change. Let Started agent runs finish or Pause them, and let the
  runner windows finish uploading Manual hand-ins.
- Deploy: `migrate`, rebuild the server image, restart Daphne, web, `mcp` and every
  Celery worker.
- Each user creates runners on `/runners/`, downloads the ZIPs and installs them
  (setup script), then assigns runners to their paused or cancelled runs.

## Phases

The compatibility layer is gone, so the work splits into **two phases**. Each can
be shipped on its own.

**Phase 1: user runners, assignment, project runner state removed.** This is one
change set that must ship together: the protocol switch, the column drop and the
re-keyed dispatch cannot be deployed separately.
- Every feature names its runner.
- Exclusivity is per runner.
- The existing per-project rules stay in place (one active run per project, one
  live setup turn per project).
- Runs of different projects already run in parallel on different runners.

**Phase 2: parallel runs within a project.** *Deferred (user, 2026-11-10): low
priority; the main need is one runner install serving every project without
changing its key. Implement Phase 1 only.*
- Remove the per-project rules: `_has_active_test_run` / `_no_active_run_check`,
  the reopen "another run active" check, and `setup_one_live_turn_per_project`,
  replaced by one live turn per runner and one per conversation.
- Audit the single-active-run lookups (A30).
- After Phase 2, two runs of one project execute at once on two runners.

## Alternatives considered

- **Runner pool or automatic scheduling.** It does more than was asked and hides
  which machine ran what. The person picks.
- **An explicit lease row set and cleared by every holder.** Runs end through more
  than ten paths, and a missed release strands the runner. The derived
  `runner_use` check under the runner row lock mirrors today's per-project checks
  and cannot leak.
- **Keep `project_id` in `controller_*` and look up the runner inside.** It hides
  which runner a call uses; MCP, setup and readiness each need a different
  source. `RunnerId` with mypy is safer.
- **One big phase.** Lifting the per-project rules is a separate audit (A30) with
  its own risk. Splitting it out keeps Phase 1's review focused on identity and
  routing.
- **Globally unique names.** They collide on obvious names. Per owner with "name
  (owner)" in shared views (Q1).
- **One generic "which runner" resolver.** Each feature resolves its runner in its
  own service (run, thread, claim). The sources differ in meaning, and a shared
  resolver would become a dumping ground.

## Risks and mitigations

- **Big-bang deploy.** Every runner stops at deploy, and users must reinstall
  (accepted by the user). Mitigations:
  - the clear refusal text with the `/runners/` URL
  - the deploy checklist
  - STARTED agent runs cancelled but resumable
  - PAUSED runs kept intact
- **Large mechanical refactor (A6).** About 130 call sites. The `RunnerId` NewType
  makes mypy strict list them all.
- **Hidden single-active-run assumptions.** They are kept safe in Phase 1 by the
  retained project rule, and audited in Phase 2 against A30. The implementer also
  greps `ACTIVE_RUN_STATUSES`, `status=TestRunStatus.STARTED` and `project_id=`
  TestRun filters.
- **Setup turn constraint change** (Phase 2). One live turn per project currently
  also serializes project-level setup writes (Application context revision,
  approvals, KB references). Those writes look conditional or row-locked, but
  this is not confirmed for every tool. The reviewer audits `setup_tools` before
  the swap.
- **Token handling.**
  - The token is shown once, never logged, and never in task args or URLs.
  - It is stored as a hash plus Fernet ciphertext; the handshake compares in
    constant time.
  - Download and reissue are owner-only POSTs, and reissue closes the live
    socket.
- **A user's machine receives another project's data.** Only the owner, who is a
  writable member of that project, can assign it (Q2, Q3). Release is always
  available to the owner.
- **Owner deactivated or loses membership mid-run.**
  - Deactivated: the handshake is refused. The run waits out the reconnect grace,
    then the existing runner-lost fault opens Need help.
  - Membership lost: work in progress continues, new assignments are refused, and
    window answers are refused.
- **Capacity.** N parallel runs multiply execution workers, vision-worker parse
  load (more `BUSY`, retries within `VISION_PARSE_BUDGET_SECONDS`), LLM
  throughput and Redis channel traffic (screenshots of several MiB). These are
  documented with no cap (Q10).
- **Migration on populated tables.** It only adds or drops columns and updates
  the active-run rows. It cancels runs and releases window claims, which
  `docs/changed/` states. The column drop is safe because Phase 1 code no longer
  reads them.
- **XSS.** Runner names, hosts and system info are user- or machine-supplied. They
  are escaped in templates and fenced as data in agent prompts.
- **N+1 queries.**
  - The `/runners/` list: one query for runners, one aggregate for active runs by
    `runner_id`, and one for live turns.
  - Project tables: `select_related("runner")` on `get_latest_runs_by_project`.

## Implementation steps

### Phase 1 (one change set; steps are reviewed in order)

1. **Shared code first.** The `runners` app, `TestRunner`, `RunnerId` and admin.
   Protocol constants: `CLIENT_VERSION` 0.25.0, `USER_RUNNER_V1` in
   `REQUIRED_CLIENT_CAPABILITIES`, handshake and ack fields.
   - Files: `runners/{apps,models,types,admin}.py` and migration,
     `auto_tester/settings.py`, `controller_client/protocol.py`.
2. **Connection and auth.** `authenticate_runner`, `runner_connection`, and the
   consumer keyed by runner (group `runner_{id}`, ack with the runner). Drop
   `api_key` and `person_email` handling.
   - Files: `runners/services/{authenticate_runner,runner_connection}.py`,
     `projects/controller_consumer.py`, `projects/controller_authenticator.py`,
     `projects/services.py` (A4).
3. **Dispatch re-keyed to `RunnerId`.** `controller_*`, reconnect services,
   `ToolContext.runner_id`, agent tools, the mobile, capture, command and web
   browser action modules, capability, browser and target helpers, and the
   management commands (A3, A5, A6, A25, A26).
4. **Machine state and status.** `runner_status` (state and broadcast),
   `RunnerStatusConsumer` and routing; remove `AgentStatusConsumer`; update the
   project tables and dashboards.
   - Files: A7, A8, A23, `static/js/punk-hazard.js`.
5. **Runs.** `TestRun.runner` fields, `runner_use.py` (replaces
   `run_machine_use.py`), `run_runner_assignment.py`, Start, Resume, Re-run, help
   Continue and person-step gates, the continue-mode rule, Release, and
   force-disconnect abort.
   - Files: `projects/models.py`, `forms.py`, `services.py`,
     `run_resume_services.py`, `run_reopen_services.py`, `run_rerun_services.py`,
     `case_pause_policy.py`, `case_help_services.py`, `person_step_services.py`,
     `views.py`, run templates.
6. **Runner window per runner.** Snapshot, answers, owner identity, throttle and
   nudges (A18, A19, `machine_setup/events.py`, `runner_person_step_push.py`).
7. **Self-update per runner.** `RunnerUpdate.runner`, the update services, and the
   panel moved to the runner detail page (A12).
8. **Machine setup.** `SetupTurn.runner`, `SetupConversation.runner`, thread runner
   resolution, gates, `run_setup_task`, the header picker and the consumer group
   (A11, A27). The project live-turn constraint is kept.
9. **Readiness.** Ports, locks, the inventory FK, the run card and tab picker, and
   tasks (A13, A14, A28).
10. **Manual runs and MCP.** Optional runner on Manual runs, window claims, the
    upload runner gate, the MCP gate, lock and `read_project`, and the
    phone-apps picker with a runner choice (A16, A17, A20).
11. **Runner pages and ZIP.** `/runners/…` views, forms and templates, `runner_zip`,
    `RUNNER_MAX_PER_USER`, the nav link, `/runners/all/`. Remove
    `download_controller_client` and `_generate_env_content`.
    - Files: `runners/{views,urls,forms}.py`, `runners/services/*`,
      `templates/runners/*`, `auto_tester/urls.py`, `templates/layouts/app.html`,
      `static/css/punk-hazard.css`, `example.env`.
12. **Onboarding and Environment.** The wizard `setup_machine` picks or adds a
    runner; `setup_run` gets the runner field; the Environment tab becomes "Test
    Runners"; the Connection key card and regenerate dialog get MCP-only wording.
    - Files: `projects/views.py`, `templates/projects/wizard/step_machine.html`,
      `step_run.html`, `templates/projects/environment.html`,
      `templates/partials/connection_key_card.html`.
13. **Controller client 0.25.0.** Runner token config and CLI, removal of the API
    key and person email (prompt included), the name in the window title.
    - Files: `controller_client/{config,client}.py`, the person window start-up
      prompt module, `controller_client/example.env`, `scripts` and setup docs.
14. **Data migrations** (see Migration and deploy). Then update `CLAUDE.md`
    wherever the Architecture, Authorization, Need help and MCP sections say "the
    project's runner", the Connection key or `CONTROLLER_PERSON_EMAIL`.

### Phase 2

15. **Per-project gates removed.** Remove the per-project active-run and reopen
    rules. Swap `setup_one_live_turn_per_project` for per-runner plus
    per-conversation constraints (migration). Audit and fix the A30 lookups: the
    Manual claim resolves by the run, not "the project's running manual run".
    - Files: `projects/services.py`, `run_reopen_services.py`, A30 modules,
      `machine_setup/models.py` and migration, `machine_setup/turn_state.py`,
      `send_gates.py`.
16. **Docs.** Update `CLAUDE.md` and the change note.

## Verification

There are no automated tests in this project.

- **Every phase.**
  - `pre-commit run --all-files`: black, isort, mypy strict for the server and
    `controller_client/`.
  - `python manage.py check` and `python manage.py makemigrations --check`.
  - `scripts/verify_server_image.sh`.
- **Phase 1, migration.** On a copy of the dev database holding one STARTED agent
  run, one PAUSED run with a help request, and one Manual run with a runner-window
  claim and an MCP claim, run `migrate`. Check that:
  - the agent run is CANCELLED and Resume works after assigning a runner;
  - the PAUSED run still shows its help card and asks for a runner on Continue,
    without current screen;
  - the window claim is released and the MCP claim is kept;
  - the `Project.agent_*` columns are gone.
- **Phase 1, runners.**
  - Two runners of one user named the same are refused; the same name for two
    users is allowed.
  - The ZIP's `.env` has `CONTROLLER_RUNNER_TOKEN` and no API key or person email.
  - A 0.24.0 runner gets the "outdated" refusal and exits.
  - Two runners of one user connect at once and both show Ready.
  - Reissue closes the socket and the old token is refused.
- **Phase 1, holds.**
  - A run of project A on runner 1 and a run of project B on runner 2 execute in
    parallel.
  - Starting a project C run on runner 1 is refused with the hold reason.
  - The runner 1 window shows only project A's steps.
  - Force disconnect of runner 1 aborts only the project A run.
  - A setup main chat on runner 2 is refused while runner 2 runs project B's run,
    and allowed on runner 3.
  - An MCP machine tool on a Manual run assigned to a busy runner returns
    `[machine_busy]`.
  - Uploads from a runner other than the hand-in's are refused.
  - Scans on two runners run at once.
- **Phase 2.**
  - Two runs of one project on two runners execute in parallel.
  - A Night auto-fix on runner 1 and a person's main chat on runner 2 in the same
    project run at once.
  - The Manual claim in a project with two active runs claims the right run.
- Run `reviewer` (SOLID, query counts on list pages, lock order) and `verifier`
  (request coverage, auth on every `/runners/` view, token never logged, no XSS
  on names) after each phase.

## Open questions

None. Q1–Q13 are resolved (see Decisions). The deploy-time handling of active runs
and window claims follows from "no backward compatibility" and is specified in
Migration and deploy; the user should confirm it in the deploy announcement.

## Changelog

- 2026-11-09: created (Draft).
- 2026-11-09: user decisions applied.
  - No backward compatibility: dropped the legacy runner rows,
    `LEGACY_PROJECT_RUNNERS`, `rebind_runner` / `RUNNER_REBIND_V1`, the "Make it
    my runner" adoption and the retire-legacy phase.
  - Runners authenticate only with a runner token; runners below 0.25.0 are
    refused (`USER_RUNNER_V1` required).
  - The project ZIP download, the `Project.agent_*` columns and
    `CONTROLLER_PERSON_EMAIL` are removed in Phase 1.
  - Specified the deploy-time handling of active runs, claims, updates and turns.
  - Re-phased into two phases; Q1–Q13 moved to Decisions.
  - Status set to Ready.
- 2026-11-10: Phase 2 deferred by the user (the main need is one runner install
  serving every project without changing its key). Phase 1 implemented in
  parallel waves (foundation, then six areas), integrated, then five
  review/verify rounds.
- 2026-11-10: review findings fixed: `runner_hold(allow=)` keeps looking past
  permitted holds; one lock helper (`runners/locks.py`) and order runner →
  project → run everywhere; Release refused for a Started agent run;
  `runner_serves_project` checked on every path that acts for a project
  (wake paths, setup sends, auto-fix, Manual claim, readiness, MCP); every
  runner switch drops screen continuations
  (`projects/screen_continuations.py`); runner hostnames never reach
  read-only members; CSRF on `only` dialog includes; admin deactivation
  closes the owner's runner sockets.
- 2026-11-10: findings rejected, with reasons:
  - Rate limiting runner-token handshakes: unchanged from the Connection-key
    handshake; tokens carry 256 bits.
  - Stopping a Started agent run when its runner's owner leaves: work in
    progress continues by design; every new assignment and wake is refused.
  - Showing the issued token in the POST response instead of a session
    flash: kept the Post/Redirect/Get flash, popped on the first
    `never_cache` render, never logged or put in a URL.
- 2026-11-10: known limit: a Started Manual run without a runner (from the
  deploy migration or a Release) cannot get one, because a Started run
  cannot switch runner (Q6). Its MCP machine tools answer `[not_set_up]`
  until the run is stopped and re-run with a runner.
- Status: Done (Phase 1).

## Interfaces (Phase 1 foundation)

Written by the foundation wave (shared code + re-keyed dispatch core). Callers
outside these files are fixed by the next wave against these signatures.

### New app `runners` (registered in `INSTALLED_APPS`)

- `runners/types.py` (leaf): `RunnerId = NewType("RunnerId", int)`;
  `IssuedRunnerToken(plaintext: str, token_hash: str, token_encrypted: str)` (frozen).
- `runners/channels.py` (leaf): `runner_group(runner_id: int) -> str` = `runner_{id}`
  (controller socket; every `controller.*` action, nudges, close events);
  `runner_status_group(runner_id) -> "runner_status_{id}"`;
  `runner_owner_group(user_id) -> "runner_owner_{user_id}"`. Event types:
  `FORCE_DISCONNECT_EVENT = "controller.force_disconnect"` (group send; consumer
  closes 4002), `SUPERSEDED_EVENT = "controller.superseded"` (sent to the replaced
  channel; consumer closes 4001), `RUNNER_STATUS_EVENT = "runner.status"`.
- `runners/models.py`: `TestRunner` — `owner` FK user PROTECT (`related_name="test_runners"`),
  `name` CharField(64), `token_hash` CharField(64, unique), `token_encrypted` TextField,
  `connected` (indexed), `channel_name`, `system_info`, `client_version`,
  `capabilities`, `device_state` (`projects.models.RunnerDeviceState`),
  `device_detail`, `device_reported_at`, `last_connected_at`, `created_at`,
  `updated_at`, `archived_at`. Properties `runner_id -> RunnerId`, `is_archived`.
  Constants `RUNNER_NAME_MAX_LENGTH = 64`, `RUNNER_TOKEN_HASH_LENGTH = 64`.
  Constraint `runner_name_unique_per_owner` = `UniqueConstraint(Lower("name"), "owner",
  condition=Q(archived_at__isnull=True))`; index (owner, archived_at).
- `runners/admin.py`: `TestRunnerAdmin(admin.ModelAdmin[TestRunner])`, read-only,
  no add/delete, token fields never listed.
- `runners/exceptions.py` (all carry `.message`): `RunnerError`,
  `RunnerNotFoundError`, `RunnerPermissionError`, `RunnerNameInvalidError`,
  `RunnerNameTakenError`, `RunnerLimitReachedError(limit)`, `RunnerHeldError(message)`,
  `RunnerAssignedError`, `RunnerNotReadyError(message)`, `RunAssignmentError(message)`.
- `runners/tokens.py`: `RUNNER_TOKEN_PREFIX = "phr_"`, `hash_runner_token(plaintext: str) -> str`
  (sha256 hex), `mint_runner_token() -> IssuedRunnerToken`.
- `runners/names.py`: `normalize_runner_name(raw: str) -> str` (raises `RunnerNameInvalidError`).
- `runners/machine_state.py` (leaf; `projects.services` imports it):
  `MachineState` (moved, same values), `DEVICE_NOT_READY_MESSAGE` (moved),
  `engine_machine_state(visual_engine) -> MachineState` (was `connected_machine_state`),
  `runner_machine_state(runner: TestRunner | None, visual_engine) -> MachineState`
  (replaces `get_machine_state(project, …)`; None = DISCONNECTED; no query),
  `device_machine_state(runner) -> MachineState | None`, `device_offline(runner) -> bool`.
- `runners/services/runner_status.py`: `RunnerStatusEvent` TypedDict (`type`, `runner_id`,
  `name`, `connected`, `machine_state`, `system_info`, `client_version`, `capabilities`,
  `last_connected_at`, `device_state`, `device_detail`, `device_reported_at`);
  `load_runner_machine_state(runner_id: RunnerId, visual_engine: VisualEngineStatus | None = None) -> MachineState`;
  `build_runner_status_event(runner, visual_engine) -> RunnerStatusEvent`;
  `broadcast_runner_status(runner: TestRunner) -> None` (to `runner_status_{id}` and
  `runner_owner_{owner_id}`, never raises). Replaces `broadcast_agent_status` /
  `AgentStatusEvent` / `agent_status_{project_id}`.
- `runners/services/runner_access.py`: `owned_runners(user) -> QuerySet[TestRunner]`,
  `usable_runners(user, project) -> QuerySet[TestRunner]` (own non-archived runners,
  only for an active **writable member**; superuser flag does not count),
  `can_assign(user, runner, project) -> bool`, `can_manage(user, runner) -> bool` (owner),
  `can_force_disconnect(user, runner) -> bool` (owner or active superuser),
  `can_view(user, runner) -> bool` (owner, superuser, or member of a project whose
  active run holds it).
- `runners/services/authenticate_runner.py`: `authenticate_runner(token: str) -> TestRunner | None`
  (sha256 lookup + `hmac.compare_digest`; None for unknown, archived or inactive owner;
  `owner` select_related); `outdated_runner_message() -> str`
  ("This Test Runner is outdated. Install a new one from <SITE_URL>/runners/").
- `runners/services/runner_connection.py`:
  `RunnerClaim(runner: TestRunner, superseded_channel: str)` (frozen);
  `claim_runner_connection(runner_id: RunnerId, channel_name: str, system_info: dict[str, Any], client_version: str, capabilities: Sequence[str]) -> RunnerClaim`
  (raises `RunnerNotFoundError` if archived meanwhile; the consumer sends
  `SUPERSEDED_EVENT` to `superseded_channel` when non-empty);
  `release_runner_connection(runner_id: RunnerId, channel_name: str) -> TestRunner | None`;
  `clear_runner_connection(runner_id) -> None`; `send_runner_close(runner_id) -> None`
  (group send `FORCE_DISCONNECT_EVENT`); `force_disconnect_runner(runner: TestRunner) -> bool`
  (clear, close, broadcast, `abort_runs_on_runner`); `abort_runs_on_runner(runner_id) -> None`
  (STARTED non-Manual runs with `runner_id`, via `projects.services.abort_test_run`,
  reason `RUNNER_DISCONNECTED_REASON`).
- `runners/services/create_runner.py`: `CreatedRunner(runner, token)`;
  `create_runner(*, owner: CustomUser, name: str) -> CreatedRunner` (owner row lock,
  `RUNNER_MAX_PER_USER`; raises name/limit errors).
- `runners/services/rename_runner.py`: `rename_runner(*, actor, runner_id: RunnerId, name: str) -> TestRunner`.
- `runners/services/reissue_token.py`: `reissue_runner_token(*, actor, runner_id: RunnerId) -> str`
  (new plaintext; a connected socket is cleared and closed with 4002; runs are not aborted).
- `runners/services/archive_runner.py`: `archive_runner(*, actor, runner_id: RunnerId) -> TestRunner`
  (refuses `RunnerHeldError` / `RunnerAssignedError`; closes the socket).
- Setting `RUNNER_MAX_PER_USER` (default 20) in `auto_tester/settings.py` and `example.env`.
- Migration `runners/0001_initial`.

### `projects`

- `projects/models.py` (migration `projects/0061_run_runner_fields`, additive):
  `TestRun.runner` FK `runners.TestRunner` null SET_NULL (`related_name="test_runs"`),
  `TestRun.runner_name_snapshot` CharField(64, blank), `TestRun.runner_assigned_at`;
  `RunnerUpdate.runner` FK null CASCADE (`related_name="updates"`; constraint/index
  still on `project` until the data migration); `ManualCaseSubmission.runner` FK null SET_NULL.
  `Project.agent_*` columns kept for now (no longer read or written by the files of this wave).
- `projects/runner_use.py` (leaf): `RunnerHoldKind` (`agent_run`, `manual_case`,
  `setup_turn`, `runner_update`, `readiness_scan`, `readiness_check`);
  `RunnerHold(kind, message, project_id: int | None, test_run_id: int | None)` (frozen,
  neutral message); `runner_hold(runner_id: RunnerId, *, now=None, ignore_run_id: int | None = None, ignore_turn_id: int | None = None) -> RunnerHold | None`;
  `agent_run_started_on_runner(runner_id) -> bool`; `manual_tester_on_runner(runner_id) -> bool`;
  `runner_update_in_flight(runner_id, now) -> bool`; `runner_id_of_run(test_run) -> RunnerId | None`.
  `projects/run_machine_use.py` kept, marked deprecated.
- `projects/run_runner_assignment.py`: `ASSIGNABLE_RUN_STATUSES` {WAITING},
  `REASSIGNABLE_RUN_STATUSES` {PAUSED, DONE, CANCELLED};
  `lock_usable_runner(runner_id: RunnerId, *, ignore_run_id: int | None = None) -> TestRunner`
  (row lock + not archived + connected + device ready + not held; no ownership; for
  Resume / help Continue by any writable member, Q3);
  `lock_runner_to_assign(runner_id, *, actor, project, ignore_run_id=None) -> TestRunner`
  (+ `can_assign`); `write_runner_assignment(test_run, runner, *, now=None) -> None`
  (run row locked by caller; nudges old + new runner after commit);
  `assign_runner(*, test_run, runner_id, actor) -> TestRun` (WAITING);
  `reassign_runner(*, test_run, runner_id, actor) -> TestRun` (locks both runners in id
  order, bumps `execution_generation`). Start / Re-run call `lock_runner_to_assign`
  first (before the project lock) and `write_runner_assignment` after locking the run.
- `projects/runner_person_step_push.py`: `nudge_runner_person_steps(runner_id: RunnerId | None) -> None`
  → group `runner_{id}`, event `controller.person_steps_changed`; None is a no-op.
  The project-keyed version is gone.
- `projects/runner_target.py`: `load_runner_target(runner_id: RunnerId) -> RunnerTarget`,
  `runner_target_of(runner: TestRunner | None) -> RunnerTarget` (replaces
  `project_runner_target(project)`); re-exports `DEVICE_NOT_READY_MESSAGE`,
  `device_machine_state`, `device_offline` from `runners.machine_state` (now take a `TestRunner`).
- `projects/services.py`:
  - every `controller_*`, `dispatch_controller_action`, `_dispatch_controller_action`,
    `_run_with_reconnect_retry`, `_current_owner_or_raise`, `_controller_group` take
    `runner_id: RunnerId` as the first argument instead of `project_id` (other params unchanged);
    group `runner_{id}`.
  - `_safe_cleanup(runner_id: RunnerId, *, wait_for_runner)`, `_machine_can_close_apps(runner_id)`,
    `_safe_teardown(pivot, project, runner: TestRunner, *, …)`; `_CaseTeardown` protocol
    (= `agents.services.case_teardown.run_case_teardown`) now takes
    `(checkpoint, project_id: int, runner_id: RunnerId, *, …)`.
  - `_nudge_runner_after_commit(test_run: TestRun)` (nudges the run's runner).
  - Removed: `MachineState` (import from `runners.machine_state`), `get_machine_state`,
    `connected_machine_state`, `claim_agent_connection`, `release_agent_connection`,
    `_clear_agent_connection`, `force_disconnect_controller`, `broadcast_agent_status`,
    `AgentStatusEvent`, `abort_active_test_run_on_disconnect`.
  - Kept: `get_project_by_api_key` (MCP), `check_controller_compatibility` (message now
    ends with `outdated_runner_message()`).
- `projects/controller_reconnect_services.py`: `wait_for_controller(runner_id: RunnerId, lost_owner: str = "") -> str`,
  `controller_connection_lost(runner_id: RunnerId, owner_channel: str) -> bool`,
  `connected_owner(runner_id: RunnerId, lost_owner: str = "") -> str | None`,
  `_has_started_run(runner_id)` (a STARTED run, any mode, with that runner).
  `no_reconnect_wait` unchanged.

### Other apps

- `agents/types.py`: `ToolContext.runner_id: RunnerId` (required, right after `project_id`).
- `machine_setup/models.py` (migration `0013_setup_runner_fields`): `SetupTurn.runner`
  (null SET_NULL, `related_name="setup_turns"`, index (runner, status)),
  `SetupConversation.runner` (null SET_NULL). `setup_one_live_turn_per_project` kept.
- `machine_setup/turn_state.py`: `runner_turn_live(runner_id: RunnerId, now, *, exclude_turn_id: int | None = None) -> bool`.
- `machine_readiness/models.py` (migration `0002_inventory_runner`): `ArtifactInventory.runner`
  (null SET_NULL); constraints `artifact_inventory_one_per_runner_os` (project, runner, os_name
  where runner set) and `artifact_inventory_one_legacy_per_os` (project, os_name where runner null).
- `machine_readiness/project_locks.py`: `RUNNER_SCAN_LOCK_PREFIX = "artifact-scan:runner:"`,
  `RUNNER_QUICK_CHECK_LOCK_PREFIX = "stage0-quick-check:runner:"`; use the existing
  `acquire/claim/is_locked/release` with the runner id. `ANALYSIS_LOCK_PREFIX` stays per project.
- `controller_client/protocol.py`: `CLIENT_VERSION = "0.25.0"`; `ClientCapability.USER_RUNNER_V1`
  (in `REQUIRED_CLIENT_CAPABILITIES`); `RUNNER_TOKEN_MAX_LENGTH = 128`;
  `HandshakePayload(runner_token, client_version, capabilities, system_info, update_in_flight_id=None)`;
  `HandshakeAckPayload(status, message, runner_id, runner_name)`;
  `parse_runner_token(data: Mapping[str, object]) -> str | None` (raises `ProtocolError`).
  Removed: `api_key`, `person_email` fields, `normalize_person_email`, `parse_person_email`,
  `PERSON_EMAIL_MAX_LENGTH`.

### Decisions and deviations

- Name uniqueness is conditional on `archived_at IS NULL`, so archiving frees a name.
- Assignment checks connected + device ready + not held, but not the visual engine
  (left to the run readiness checks).
- `usable_runners` / `can_assign` require a real writable membership; a superuser's
  implicit project access does not count (the runner window lists memberships only).
- `force_disconnect_runner` aborts STARTED **agent** runs only; a Manual run keeps going
  (its runner is optional, Q12). Token reissue closes the socket but does not abort runs
  (STARTED runs wait out the reconnect grace).
- `runner_hold(ignore_run_id=)` ignores only that run's agent-run hold, never a Manual claim.
- Lock key helpers for scan/quick-check are new prefixes, not new functions.
- `.env` in this checkout has no `FIELD_ENCRYPTION_KEY`; mypy/manage.py were run with a
  throwaway key in the environment. `makemigrations` needs `--skip-checks` until callers compile.
