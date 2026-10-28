# feat: Update the Test Runner from the panel (side-by-side install)

Status: Done
Created: 2026-10-28
Request: Updating the Test Runner (`controller_client`) by hand on every test machine is
slow and error-prone. When the connected runner reports a version older than the server's
`CLIENT_VERSION`, a project member should be able to press **Update** on the panel and have
the runner update itself. **Binding user decision (2026-10-28):** the update installs the new
runner *side by side*. The runner gets the new `controller_client` into a new, separate
folder, installs that folder's own venv, requirements and (if needed) Playwright browsers,
and starts the new runner from there. The new process connects and the old one exits. The
new process then removes the old folder. There is no rollback, no watchdog and no kept
previous copy: if the new runner fails, a person handles it.

## Context

- **Protocol** (`controller_client/protocol.py`, stdlib only; the server imports it):
  `CLIENT_VERSION = "0.7.0"`, `MessageType`, `ClientCapability` (optional ones are checked
  where they are used), `REQUIRED_CLIENT_CAPABILITIES` (checked at handshake by
  `projects.services.check_controller_compatibility`), and `CloseCode.SUPERSEDED = 4001` /
  `FORCE_DISCONNECTED = 4002`, which are terminal for the client.
- **Single runner per project, newest connection wins:**
  - `projects.services.claim_agent_connection` locks the project row and makes the new
    channel the owner.
  - `ControllerConsumer._establish_authenticated_connection` then sends
    `controller.superseded` to the previous channel. That channel closes with 4001.
  - `ControllerClient._raise_if_terminated` turns 4001 into `ConnectionTerminatedError`.
    `run()` stops without reconnecting and `main()` calls `client.stop()`, which closes the
    browser, kills tracked processes and ends interactive sessions. Then the process exits.
  - **So the handover needs no new server logic.** The new runner connecting is what makes
    the old one exit.
- **Runner process** (`controller_client/main.py`, `client.py`, `config.py`):
  - Started by hand from the folder that contains `controller_client/` (the "install root"),
    with `controller_client/.venv/bin/python -m controller_client.main` (Windows:
    `controller_client\.venv\Scripts\python`).
  - Config: `controller_client/.env` (read through python-decouple next to `config.py`, so
    the cwd does not matter), with CLI overrides.
  - Logs go to stderr only; there is no log file.
  - It handles one server message at a time (`_receive_messages` awaits each handler), so a
    long update must run as a background task.
  - The client's `MAX_MESSAGE_SIZE` is 10 MiB.
  - No autostart (`docs/changed/2026-10-28-wayland-controller.md`).
  - On Windows it is run as Administrator when tests drive elevated apps.
- **Install** (`controller_client/scripts/setup.{sh,ps1,bat}`):
  - Python 3.13+ check, `.venv` inside `controller_client/`, and
    `pip install -r requirements.txt`.
  - `playwright install --with-deps`. On Linux `--with-deps` uses sudo/apt, and `setup.sh`
    also apt-installs the GStreamer packages.
  - Copies `example.env` to `.env`, then runs a certifi self-check.
  - Playwright browsers live in the per-user Playwright cache, shared by every venv, so
    reinstalling the same Playwright version downloads nothing.
- **ZIP download:**
  - `projects.views.download_controller_client` (POST, `@project_membership_required`).
  - It calls `projects.services.generate_controller_client_zip(project)`, which walks
    `BASE_DIR/controller_client`. It skips `.venv`, `__pycache__`, caches, `tests`,
    `weights` and `.env`, then writes a generated `.env` that holds the project's
    Connection key.
  - The tracked source stays in the server image (`.dockerignore`,
    `scripts/verify_server_image.sh`).
  - The source is about 420 KB uncompressed, so the ZIP is roughly 120–150 KB.
- **Version and outdated state on the panel:**
  - `Project.agent_client_version` and `agent_capabilities` are written by
    `claim_agent_connection`.
  - They are broadcast as `agent.status` to `AgentStatusConsumer` (`projects/consumers.py`)
    and shown as text in `templates/projects/includes/_environment_connection.html`
    ("Test Runner version").
  - Nothing compares that version with the server's. `MachineState` has no "outdated" state.
  - `RunnerOutdatedError` → `BlockedReason.RUNNER_OUTDATED` (`projects/case_faults.py`)
    pauses a case with a help request that asks "Update the Test Runner on the test machine,
    then Continue". Its continue mode is `current_screen`.
  - `projects/runner_capability_services.py` raises it for missing optional capabilities.
- **Machine-busy gates today:**
  - `start_test_run` locks the project row, then checks for an active run and
    `live_turn_exists` (`machine_setup/turn_state.py`).
  - `run_reopen_services.list_reopen_blockers` covers Resume, re-run and redesign.
  - Machine setup sends refuse in `machine_setup/services/send_message._refusal` while a run
    is STARTED (a PAUSED run is allowed), a turn is live, or the machine is not READY.
  - `case_auto_fix` checks for a live turn and a READY machine.
  - The readiness checklist (`get_run_readiness`) has a check per gate.
- **Concurrent work:** `docs/plan/feat-hitl-runner-chat.md` (Human in the loop through the
  Test Runner) is being planned in parallel. It touches the same places. See *Shared touch
  points*.

## Goals
- An **Update Test Runner** button on the panel, shown when the connected runner is older
  than the server's `CLIENT_VERSION` and can update itself.
- The runner downloads the server's own bundle over its authenticated socket, verifies it,
  and installs it into a new folder with its own venv. It runs Playwright install, starts
  the new runner detached, and hands the connection over. The new runner deletes the old
  folder.
- Live status on the panel. The update is refused while the machine is busy, and runs,
  resumes and setup turns are refused while an update is in flight.
- Each update is recorded (who, from/to, outcome, detail).
- Later updates and the setup scripts keep working with the new folder layout.

## Non-goals
- Rollback, watchdog, keeping the previous copy, or a supervisor/launcher process (user
  decision).
- Updating runners older than the release that adds this feature. They need one last manual
  update.
- Installing OS packages (apt GStreamer, `playwright install --with-deps` on Linux), or
  upgrading Python itself.
- Autostart or service installation for the runner.
- Updating `vision_worker`. It has its own protocol, no Playwright, and one process per GPU.
  The same design could be reused there later, but not in this plan.
- Updating automatically without a person pressing the button.

## Design

### Shared touch points (coordinate with `feat-hitl-runner-chat`)
- `controller_client/protocol.py`: both plans add `MessageType` members and a
  `ClientCapability`, and both bump `CLIENT_VERSION`. Whichever lands second rebases and
  bumps again (for example 0.8.0, then 0.9.0). Capabilities are additive and independent.
- `controller_client/client.py`: both add entries to `self._handlers` and possibly a
  post-ack hook. Keep edits to separate, appended blocks.
- `projects/controller_consumer.py` (`_route_authenticated_message` handlers dict, new
  `controller_*` event methods) and `projects/controller_protocol.py`
  (`ActionTypeRegistry`): both add entries.
- `controller_client/config.py` / `main.py`: this plan adds CLI flags and file logging. If
  the HITL plan adds runner UI, it must not depend on the old process surviving an update.
- Recommended order: land whichever is ready first. The second rebases the four files
  above. Neither plan should edit the other's new modules.

### Protocol (`controller_client/protocol.py`)
- `CLIENT_VERSION = "0.8.0"` (or the next free version, see above).
- `ClientCapability.SELF_UPDATE_V1 = "self_update_v1"` (optional, checked by the update
  service).
- `MessageType.UPDATE_RUNNER = "update_runner"` (server → runner) and
  `MessageType.UPDATE_STATUS = "update_status"` (runner → server).
- `UpdateRunnerPayload`:
  - `update_id: str` (server UUID)
  - `target_version: str`
  - `archive_base64: str`
  - `archive_sha256: str` (hex)
  - `archive_size: int`
  - `parse_update_runner_payload` enforces `archive_size <= MAX_UPDATE_ARCHIVE_BYTES`
    (8 MiB) before decoding.
- `UpdatePhase(StrEnum)`, sent in `update_status`:
  - `accepted`, `preparing` (verify and extract), `creating_environment`,
    `installing_dependencies`, `installing_browsers`, `starting` (new process spawned),
    `waiting_for_new_runner`
  - `failed` (terminal, runner side)
  - `old_removed` / `old_removal_failed` (sent by the *new* runner after it connects;
    informational)
- `UpdateStatusPayload(update_id, phase, detail)`, where `detail` is plain text of at most
  500 characters.
- `version_tuple(version: str) -> tuple[int, ...] | None`: a pure helper that both sides use
  to compare versions. `None` means the version cannot be parsed.

### Folder layout (runner side)
- **Base folder:**
  - If the current install root's name matches the managed pattern `test-runner-<version>`
    (optionally with `-<n>`), the base is that folder's parent.
  - Otherwise the base is the current install root. That is the folder the person unzipped
    into (possibly `~` or `C:\Users\x`).
- **New install root:** `<base>/test-runner-<target_version>/`, with
  `controller_client/` inside. If the name exists (an earlier failed attempt), add `-2`,
  `-3`, and so on. The folder is created with `exist_ok=False`.
- **Results:**
  - First update from a manual install: `ROOT/test-runner-0.8.0/controller_client`.
    `ROOT/controller_client` is deleted afterwards.
  - Later updates: `ROOT/test-runner-0.9.0`, and the whole `ROOT/test-runner-0.8.0` is
    deleted. Folders do not nest, and the base folder never changes.
- **Old folder to delete:**
  - Managed root: the whole old root.
  - Manual install: only `ROOT/controller_client`. The person's own folder is never
    deleted.
- **Stable start script:** each update (re)writes `<base>/start-test-runner.sh` (POSIX) or
  `<base>\start-test-runner.cmd` (Windows). It `cd`s into the current install root and runs
  its venv Python with `-m controller_client.main`. Shortcuts, scheduled tasks and the
  instructions on the page can point at it, so they keep working after the old folder is
  gone.
- **Setup scripts:** `scripts/setup.*` already resolve everything relative to their own
  folder, so running them inside a `test-runner-*` folder works unchanged. They only get an
  extra line telling the person about `start-test-runner`.
- **What is carried over:**
  - `controller_client/.env` is copied byte-for-byte. It holds the server host and port,
    the Connection key, the cleanup folder and the log level. On POSIX the copy uses mode
    0600.
  - The old process's CLI overrides (`--host`, `--port`, `--api-key`, `--log-level`,
    `--cleanup-dir`, reconnect flags) are re-passed to the new process. Update flags are
    not.
  - Logs are not carried over (the old runner has none on disk).
  - Nothing else: no venv, no `__pycache__`.
- **Disk check:** before extracting, require at least 1.5 GiB free on the base folder's
  volume (`shutil.disk_usage`). A venv is about 300 MB, and a new Playwright version
  downloads its browsers.

### Runner update flow (old process)
New modules, all stdlib plus what the venv already has. Windows code goes under
`sys.platform == "win32"` so the win32 mypy pass checks it:
- **`controller_client/update_archive.py`:**
  - Decode base64 and check the length.
  - Compare sha256 with `hmac.compare_digest`.
  - Extract safely, entry by entry. Never use `extractall`.
  - Read the target version from the extracted `protocol.py` by regex. It is never
    imported.
- **`controller_client/update_layout.py`:**
  - Base, new-root and old-target computation, plus the "is this a deletable runner folder"
    validation.
  - The start-script text and the atomic write of the start script (temp file +
    `os.replace`).
- **`controller_client/update_installer.py`:** async subprocess steps.
  - `<old venv python> -m venv <new>/controller_client/.venv`. Running venv from a venv
    builds on the base interpreter; confirm this on each OS.
  - `<new python> -m pip install -r requirements.txt` with stdin set to DEVNULL. No
    `--upgrade pip`, so pip is never asked to replace itself.
  - The certifi self-check (as in `env_check.verify_environment`), run in the new venv.
  - `<new python> -m playwright install`, with no `--with-deps`. It does nothing when the
    version's browsers are already in the shared cache.
  - Each step has a timeout: venv 5 min, pip 20 min, Playwright 30 min. On failure, the
    detail is the last lines of output (tail kept in memory, truncated to 500 characters).
- **`controller_client/update_handover.py`:** starts the new process detached.
  - Command: `[<new python>, "-m", "controller_client.main", *carried_args,
    "--update-id", id, "--replaces-pid", pid, "--replaces-pid-created", ts,
    "--remove-old-install", <old target>]`, with `cwd=<new root>`. `-m` needs the new root
    on `sys.path`.
  - **Windows:**
    - `creationflags = CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP`, plus
      `CREATE_BREAKAWAY_FROM_JOB` when allowed. If that fails with access denied, retry
      without it and log it.
    - The new console window shows the new runner's output and is independent of the old
      terminal. Closing the old window or Ctrl+C there does not reach it.
    - A child made with `CreateProcess` inherits the parent's token, so a runner started as
      Administrator stays elevated.
  - **macOS / Linux:**
    - `start_new_session=True` (no SIGHUP from the old terminal), stdin from `/dev/null`,
      stdout/stderr to `<new>/controller_client/logs/runner-start.log`.
    - The environment is inherited. That carries `DISPLAY`, `WAYLAND_DISPLAY`,
      `DBUS_SESSION_BUS_ADDRESS` and `XDG_*`, which GNOME's Mutter remote-desktop session
      needs.
    - The process then runs in the background. The old terminal prints where it lives,
      where its log is, and how to stop it: **Disconnect test machine** on the panel, or
      `kill <pid>`.
    - Note: if a person runs the runner under a systemd unit with
      `KillMode=control-group`, the new process dies with the old one. No unit exists
      today, so this is documented only.
- **`ControllerClient` (`client.py`):**
  - `_handle_update_runner` checks these, in order:
    - no update task already running (else error `EXECUTION_FAILED` "An update is already
      in progress");
    - `target_version` is newer than its own;
    - a valid payload.
  - It then sends `update_status accepted` and starts a background `asyncio.Task`, so the
    message loop keeps answering pings and actions.
  - The task sends a status at each phase.
  - If a step fails, it sends `failed` with the detail and deletes the partly built new
    folder (best effort). The old runner keeps running.
  - On success it spawns the new process and sends `starting`, then
    `waiting_for_new_runner`.
  - It then waits up to `HANDOVER_TIMEOUT_SECONDS` (300 s) for the server to supersede it.
    Superseding is the normal exit through `ConnectionTerminatedError`, and the log line
    becomes "Updated to X; the new Test Runner runs from <path>".
  - If no supersede arrives in time, see Q3.
  - If the old runner's socket drops and reconnects during the update, it re-sends the
    current phase after the ack.
- **`ClientCapability.SELF_UPDATE_V1`** is advertised by every runner of this version or
  newer.

### Runner flow (new process)
- **`config.py`:**
  - New optional flags `--update-id`, `--replaces-pid`, `--replaces-pid-created`,
    `--remove-old-install`, `--log-file`.
  - When `--update-id` is given, logging also goes to a `RotatingFileHandler` at
    `<root>/controller_client/logs/runner.log` (5 MB × 3) through `setup_logging`, which is
    extended.
- **`controller_client/update_cleanup.py`** runs after the first `ok` handshake ack, in a
  worker thread:
  - Wait (up to 120 s) until the old PID has exited. Use psutil, matching the create time
    so a reused PID is never confused for it.
  - Validate the target:
    - it is an existing directory;
    - it is not the new root, and not an ancestor or descendant of it;
    - it is not the home directory, a drive root or the base folder;
    - it is either named `controller_client` and contains `protocol.py` and `.venv`, or it
      is a managed `test-runner-*` folder that contains `controller_client/protocol.py`.
  - Delete it with `shutil.rmtree`. The `onexc` handler clears read-only bits. Retry 5
    times with backoff for transient Windows locks (antivirus, indexer).
  - Send `update_status old_removed` or `old_removal_failed` with the path.
  - This is the only code that deletes a folder. The path comes from the old process's own
    `Path(__file__)`, never from the server.
- `cleanup.default_protected_paths` already protects the running install. It needs no
  change.

### Data model (`projects/models.py`)
- `RunnerUpdateStatus(TextChoices)`:
  - non-terminal: `sent`, `preparing`, `installing`, `starting`, `waiting`;
  - terminal: `succeeded`, `failed`, `expired`.
  - Runner phases map onto these. `creating_environment`, `installing_dependencies` and
    `installing_browsers` all map to `installing`, and `phase` is stored separately for the
    label.
- `RunnerUpdate`:
  - `id` UUID primary key (sent as `update_id`)
  - `project` FK, CASCADE
  - `requested_by` FK user, SET_NULL
  - `from_version`, `to_version` (`max_length=32`)
  - `status`, `phase` (last runner phase)
  - `detail` (`max_length=500`, runner text, rendered escaped)
  - `old_removal` (`""`, `removed`, or `failed`)
  - `archive_sha256`
  - `created_at`, `updated_at`, `finished_at`
- `UniqueConstraint(fields=["project"], condition=Q(status__in=NON_TERMINAL), name=...)`
  allows one in-flight update per project.
- Additive migration on a new table. No data migration.
- Admin: read-only `ModelAdmin[RunnerUpdate]` (audit).

### Services (projects app; one use case per module)
- **`projects/runner_bundle.py`:** moves the file walk out of `projects/services.py`.
  - `build_runner_archive(env_content: str | None) -> bytes`. The update bundle has no
    `.env`.
  - `update_bundle() -> RunnerBundle(archive, sha256, version)` uses `functools.cache`,
    because the source is immutable inside the image. In dev, restart the server after
    editing the runner.
  - `generate_controller_client_zip(project)` stays as a thin wrapper, so the download view
    is unchanged.
  - The exclude list gains `logs` and `test-runner-*`.
- **`projects/runner_update_state.py`** (leaf module: ORM and settings only, imports
  nothing from `projects.services`):
  - `runner_update_in_progress(project_id, now) -> bool`: true for a non-terminal row newer
    than `RUNNER_UPDATE_TIMEOUT_SECONDS`.
  - `RUNNER_UPDATE_RUNNING_MESSAGE`.
  - `expire_stale_runner_updates(project_id, now)`: lazy expiry, called under the lock by
    the request and offer services. No beat task.
- **`projects/runner_update_offer.py`:** `get_runner_update_offer(project) ->
  RunnerUpdateOffer`, a frozen dataclass used as template context.
  - `state` is one of: `up_to_date`, `available`, `manual_required` (older, no capability),
    `blocked` (with reasons), `in_progress`, `last_failed`, `last_expired`, `not_connected`.
  - It also carries `server_version`, `runner_version`, `latest: RunnerUpdate | None`, and
    `blockers: tuple[str, ...]`.
  - It costs 2 queries: the latest update, and the gates that share the project row the
    view already has.
- **`projects/runner_update_request.py`:** `request_runner_update(project, user,
  expected_version) -> RunnerUpdate`.
  - Runs in `transaction.atomic` and locks the Project row first, the same lock order as
    `start_test_run` and `_send_locked`.
  - Checks every gate below. On refusal it raises `RunnerUpdateRefusedError(reason)`.
  - Creates the row, and on commit does `group_send` to `controller_<id>` with
    `{"type": "controller.update_runner", update_id, target_version, archive_base64,
    archive_sha256, archive_size}`, sent to the current owner channel only.
  - Broadcasts the update state.
  - `expected_version` must equal `CLIENT_VERSION`, so a page left open across a deploy
    cannot send a bundle the person did not see.
- **`projects/runner_update_progress.py`:**
  - `record_runner_update_status(project_id, update_id, phase, detail)`, from the consumer.
    It accepts only an update of *this* project that is non-terminal, or terminal for
    `old_removed*`. It maps the phase and truncates the detail. `failed` sets
    `finished_at`.
  - `settle_runner_update_on_handshake(project_id, client_version)`, from the handshake
    path. If the project has a non-terminal update with `to_version == client_version`,
    it is set to `succeeded`.
  - Both broadcast.
- **Domain errors** in `projects/exceptions.py`: `RunnerUpdateRefusedError`.

**Gates** (all in `request_runner_update`, under the project lock):
1. The runner is connected (`agent_connected` and an owner channel).
2. `SELF_UPDATE_V1` is in `agent_capabilities`. Otherwise refuse with "This Test Runner is
   too old to update itself. Download and set it up once from this page; later versions
   update with one click."
3. `version_tuple(runner) < version_tuple(CLIENT_VERSION)`. An unparseable runner version
   counts as older.
4. No run in STARTED status.
5. No live setup turn (`live_turn_exists`). This covers the main chat, help chat, result
   chat and Night auto-fix.
6. No case in `awaiting_person` (HITL: a person may be using the machine and the runner's
   browser).
7. No in-flight update (after lazy expiry).
8. A PAUSED run is allowed. See Q2 for the help-request continue-mode adjustment.

**Reverse gates** (a new check where `live_turn_exists` is checked today):
- `start_test_run`
- `run_reopen_services.list_reopen_blockers`, which covers Resume, re-run, redesign and
  help Continue
- `machine_setup/services/send_message._refusal`
- `machine_setup/services/case_auto_fix`
- a new readiness check `_no_runner_update_check` in `get_run_readiness`
- `projects/run_reopen_services.py` gets the message added the same way
  `MACHINE_SETUP_RUNNING_MESSAGE` was added.

### Pages and forms
| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `projects/<id>/runner-update/` (`projects:runner_update`) | POST | `@project_membership_required`, CSRF | `RunnerUpdateForm` (hidden `expected_version`, required) | Redirects to `projects:environment` with a flash message | `RunnerUpdateRefusedError` → `messages.error(reason)`. Invalid form → error "Reload the page and try again." |
| `projects/<id>/environment/` (existing) | GET | unchanged | — | adds `runner_update: RunnerUpdateOffer` | — |

- **View:** a thin new module `projects/runner_update_views.py`.
- **Throttling:** the one-in-flight constraint already prevents a burst of updates.
- **Connection card** (`_environment_connection.html`):
  - The "Test Runner version" row gains a badge: "Update available (0.8.0)" or "Up to
    date".
  - When `available`: an **Update Test Runner** button opens a confirm dialog
    (`partials/confirm_dialog.html`). The dialog says the runner downloads about 300 MB of
    dependencies, restarts in a new folder, closes the browsers and apps it opened, and
    that the old folder is deleted.
  - When `blocked`: the reasons are listed.
  - When `manual_required`: a short note pointing at the download card below.
  - In-flight: a live phase line ("Installing dependencies…") with an elapsed timer.
  - Terminal: an outcome alert (succeeded / failed with the escaped detail / did not
    finish). On failure it says the old runner may still be connected, or nothing may be,
    and to check the test machine and the log at `<new folder>/controller_client/logs/`.
- **Live updates:** `environmentLive()` handles a new `runner_update` WebSocket message, so
  the card updates without a reload. Initial state comes from `json_script` of the offer
  payload.
- **Runner-outdated help card** (`_case_help_card.html`): when
  `help_request.fault == "runner_outdated"`, add the link "Update the Test Runner on the
  Environment page".
- **Download card:** one line telling people that a runner of this version or newer
  updates from the Connection card.

### Background work and WebSockets
- **No Celery task.** The request only writes a row and does a `group_send` (bundle cached,
  about 200 KB base64). The runner does the slow work.
- **Consumer** (`projects/controller_consumer.py`):
  - new `controller_update_runner(event)` forwards the message through
    `ControllerMessageBuilder` (registry entry `controller.update_runner` →
    `MessageType.UPDATE_RUNNER`, payload keys listed);
  - new route `"update_status"` → `sync_to_async(record_runner_update_status)` with fields
    parsed by `parse_update_status_payload` (bad payload → logged and ignored);
  - after `claim_connection` in `_establish_authenticated_connection`,
    `settle_runner_update_on_handshake`.
- **Event** to the `agent_status_<id>` group: `{"type": "agent.runner_update",
  "runner_update": RunnerUpdatePayload}`. `AgentStatusConsumer` gains `agent_runner_update`
  and includes the payload in `_send_initial_state`.
- **Expiry:** a row older than `RUNNER_UPDATE_TIMEOUT_SECONDS` is treated as expired by the
  gates and the offer. It is written `expired` lazily on the next offer or request.

### Settings / infrastructure
- `RUNNER_UPDATE_TIMEOUT_SECONDS` (default `UPDATE_MAX_SECONDS` + 300 = 3900; below + 60 is
  refused at settings load) in `settings.py` via decouple, and added to `example.env`.
- The 7 MiB `MAX_UPDATE_ARCHIVE_BYTES` lives in the protocol, not in settings, because both
  sides must agree on it.
- `.gitignore` / `.dockerignore` / ZIP excludes: `controller_client/logs/`.
- `scripts/verify_server_image.sh`: also import `projects.runner_bundle` and build the
  update bundle once.

### Data flow
1. A member opens Environment. The view adds `get_runner_update_offer(project)`. The card
   shows "Update available".
2. The member confirms. A POST to `projects:runner_update` runs `request_runner_update`
   (lock, gates, row `sent`, on commit `group_send` + broadcast).
3. The consumer forwards `update_runner`. The old runner validates it, replies
   `accepted`, and starts the background task: verify sha256 → safe extract into
   `<base>/test-runner-X/` → copy `.env` → venv → pip → certifi check → Playwright
   install. Each phase sends `update_status`, which reaches the consumer, then
   `record_runner_update_status`, then the panel.
4. The old runner writes the start script, spawns the new process detached, and sends
   `starting` / `waiting_for_new_runner`.
5. The new runner handshakes with version X. `claim_agent_connection` makes it the owner.
   `settle_runner_update_on_handshake` marks the update `succeeded`. The old channel gets
   `controller.superseded` → 4001 → the old runner stops and exits.
6. The new runner waits for the old PID to exit, validates the old folder and deletes it,
   then sends `old_removed`. The panel shows "Updated to X".

## Alternatives considered
- **In-place file swap with a supervisor and rollback:** rejected by the user. It also
  fights Windows `.pyd` locks.
- **HTTPS download URL with a signed token:** adds an unauthenticated-session endpoint, a
  token lifecycle and TLS validation on the runner. The authenticated socket already
  carries messages of several MiB. The bundle is about 150 KB.
- **Signing the bundle (Ed25519):** the trust root is the server either way. A server (or
  anyone holding the socket) can already run arbitrary shell commands through
  `run_command`. The sha256 guards against corruption. Signing would add a crypto
  dependency and key management for no change in who can run code. It is noted as a
  possible hardening step.
- **The server diffs requirements and skips pip:** a fresh venv always needs a full
  install, so there is nothing to skip. Playwright install is idempotent and fast when the
  browsers are cached, so no diffing is needed there either.
- **`MachineState.UPDATING`:** it would need a per-project query wherever machine state is
  computed for lists (N+1). Explicit gates at the few entry points are cheaper and clearer.

## Risks and mitigations
- **Arbitrary code / wrong source:**
  - The runner installs only what arrives on its own authenticated socket. There is no URL
    field.
  - It checks the hash and size, and the extracted `protocol.py` version must equal
    `target_version`.
  - The connection is unencrypted only where `ws://` is already used (port ≠ 443, dev).
    This does not widen what `run_command` already allows.
- **ZIP hazards:** every entry must:
  - be under `controller_client/`;
  - not be absolute, contain a drive letter, `..` or a backslash;
  - not be a symlink (checked through `external_attr`);
  - not be `.env`, `.venv` or `logs`.
  - Limits: at most 1000 entries, 32 MiB uncompressed in total, 8 MiB per file. Files are
    written through an open-exclusive into a fresh directory.
- **Deleting the wrong folder:** the path comes from the old process's own location, the
  new process checks it against several rules, and the person's own base folder is never
  deleted.
- **The new runner never connects** (pip/PyPI unreachable is caught earlier; a crash at
  start, an outdated OS package, or a missing TCC/Wayland permission happens after the
  spawn):
  - The panel shows `waiting` and then `expired` or `failed`.
  - The old runner's behaviour is Q3.
  - The new process's output is in the new console window (Windows) or `logs/` (POSIX).
- **Lost state on a PAUSED run:** the old runner's `stop()` closes its browser and the
  processes it launched. That is why `current_screen` help requests need adjusting (Q2).
- **macOS permissions:**
  - Screen Recording and Accessibility are granted to the app responsible for the process
    (Terminal).
  - A detached child normally keeps that responsible process, but this must be verified.
  - If it is lost, screenshots fail after the update and the runner logs it at startup
    (`verify_screen_capture`).
- **Windows:**
  - Antivirus can hold transient file locks, so deletes retry.
  - Job objects with kill-on-close are handled with a breakaway fallback.
  - The elevation token is inherited.
  - The new console window is a visible change for the person.
- **Linux:** new system packages cannot be installed. A release that needs them must say so
  in its change note and keep the manual path (Q5).
- **Disk use:** the free-space check up front. The old folder is removed after the
  handover. Old Playwright browser revisions are cleaned up by Playwright when no install
  links them.
- **Long request:** none. The view does one row write and a `group_send` of about 200 KB.

## Implementation steps
Shared code first (one agent, before the parallel parts):
1. **Protocol and shared types** (files: `controller_client/protocol.py`,
   `controller_client/exceptions.py`):
   - version bump, capability, two message types, payload dataclasses and parsers,
     `UpdatePhase`, `MAX_UPDATE_ARCHIVE_BYTES`, `version_tuple`;
   - the `UpdateError` exception.
2. **Server model and leaf state** (files: `projects/models.py`, migration,
   `projects/admin.py`, `projects/exceptions.py`, `projects/runner_update_state.py`,
   `auto_tester/settings.py`, `example.env`).

Parallel parts. Each owns its files:

3. **A. Server services** (files: `projects/runner_bundle.py`,
   `projects/runner_update_offer.py`, `projects/runner_update_request.py`,
   `projects/runner_update_progress.py`, and the move/wrapper in `projects/services.py`
   (ZIP section only)).
   - Exposes `get_runner_update_offer`, `request_runner_update`,
     `record_runner_update_status`, `settle_runner_update_on_handshake`,
     `RunnerUpdatePayload` (TypedDict) and `broadcast_runner_update(project_id)`.
4. **B. Socket plumbing** (files: `projects/controller_consumer.py`,
   `projects/controller_protocol.py`, `projects/consumers.py` (`AgentStatusConsumer`)).
   - Consumes A's functions by name.
5. **C. Page** (files: `projects/runner_update_views.py`, `projects/forms.py`
   (`RunnerUpdateForm`), `projects/urls.py`, `projects/views.py` (environment context
   only), `templates/projects/environment.html`,
   `templates/projects/includes/_environment_connection.html`,
   `templates/projects/includes/_case_help_card.html`,
   `templates/partials/runner_download_card.html`, `static/css/punk-hazard.css` if a class
   is needed).
6. **D. Reverse gates** (files: `projects/services.py` (`start_test_run`, readiness check
   only), `projects/run_reopen_services.py`, `machine_setup/services/send_message.py`,
   `machine_setup/services/case_auto_fix.py`, `machine_setup/exceptions.py` if a new
   refusal type is needed).
   - D and A both touch `projects/services.py` in disjoint sections. Run them one after the
     other, or give the ZIP move to D.
7. **E. Runner update modules** (files: `controller_client/update_archive.py`,
   `update_layout.py`, `update_installer.py`, `update_handover.py`, `update_cleanup.py`).
   Interfaces:
   - `verify_and_extract(payload, dest_root) -> ExtractedBundle`
   - `plan_layout(current_package_dir: Path, target_version: str) -> UpdateLayout(base,
     new_root, old_target)`
   - `async install(layout, python: Path, on_phase) -> Path`, which returns the new venv
     python
   - `spawn_new_runner(new_python, new_root, args) -> SpawnedRunner(pid, create_time,
     log_path)`
   - `remove_old_install(target, new_root, replaces_pid, created) -> bool`
   - `write_start_script(base, new_root)`
8. **F. Runner wiring** (files: `controller_client/client.py`, `config.py`, `main.py`):
   - handler, background task, status sends, handover wait, post-ack cleanup trigger and
     phase resend;
   - new CLI flags and file logging.
9. **G. Docs and scripts** (files: `controller_client/scripts/setup.{sh,ps1,bat}`
   (`start-test-runner` hint only), `.gitignore`, `.dockerignore`,
   `scripts/verify_server_image.sh`, the `CLAUDE.md` controller_client paragraph).
10. **Integration** (orchestrator):
    - URL and consumer routing, registry keys versus the payload;
    - `pre-commit run --all-files`, including `mypy-controller-client-win32`;
    - `python manage.py check`, `makemigrations --check`.

## Verification
- Static: `pre-commit run --all-files` (server mypy, controller_client mypy on host and
  win32), `python manage.py check`, `python manage.py makemigrations --check`, and
  `scripts/verify_server_image.sh`.
- Manual, server side (dev, a runner on 0.8.0 started with a locally lowered
  `CLIENT_VERSION` copy):
  - The button shows only for an older runner with the capability. An older runner without
    it shows the manual note. An up-to-date runner shows "Up to date".
  - Each gate refuses with its message: STARTED run, live setup turn, awaiting-person case,
    a second click. Each reverse gate refuses while an update is in flight: Start, Resume,
    machine setup send.
  - A stale `expected_version` is refused.
  - Phases appear live. A forced pip failure (bad index URL) shows `failed`, the old runner
    keeps working and the new folder is gone.
  - `expired` appears after the timeout.
- Manual, runner side, per OS (Windows elevated and non-elevated, macOS, Ubuntu 26.04
  GNOME Wayland):
  - First update from a manual install, then a second update from a `test-runner-*`
    folder. Check the folder names, that the old folder is deleted, that the base folder is
    kept, and that `start-test-runner` launches the current version.
  - Windows: the new console window opens, elevation is kept, closing the old window does
    not kill the new runner, and the old folder is deleted after the old process exits.
  - macOS: screenshots and input still work after the update (TCC).
  - GNOME: the remote-desktop session starts in the new process. The log file is written.
    Stopping works through panel Disconnect.
  - A tampered hash and a ZIP with `../x`, a symlink or an oversize file are rejected
    before anything is written.
  - The `.env` content is identical in the new folder. On POSIX it has mode 0600.
- Run the `reviewer` and `verifier` agents (give `verifier` the original request, the
  binding side-by-side decision and this plan).

## Open questions
- **Q1. Where is the button?** Recommendation: on the Environment page Connection card,
  with an "Update available" badge next to the version. Also add a link from the
  Runner-outdated help card. Do not put it on the machine badge in the project header.
  (Draft assumes this.)
- **Q2. Allow updating while a run is PAUSED (Needs help)?** Recommendation: yes, unless a
  setup turn is live or a case is awaiting a person. This is the natural fix for a
  Runner-outdated pause. Because the handover closes the browsers and apps the old runner
  opened:
  - add `RUNNER_OUTDATED` to `case_faults._RESTORE_FAULTS`;
  - when an update is requested, switch the paused case's open help request from
    `current_screen` to `restore` if it has a checkpoint.
  (Draft assumes this. The alternative is to refuse while PAUSED.)
- **Q3. What if the new runner never connects?** Recommendation, consistent with "no
  rollback, a person handles it":
  - The old runner exits only when superseded, so until then it stays connected and
    usable.
  - After 5 minutes without a handover it reports `failed` ("new Test Runner did not
    connect; see <path>/logs"), terminates the new process it spawned, and leaves the new
    folder in place for diagnosis. No files are restored or copied back.
  - Alternative: the old runner exits as soon as it has spawned the new one. The machine is
    then offline until a person intervenes. (Draft assumes the recommendation.)
- **Q4. How is the new runner visible on macOS/Linux?** Recommendation:
  - Run in the background, with logs in `test-runner-X/controller_client/logs/`.
  - Stop it from the panel (Disconnect test machine) or with `kill`.
  - Start it next time with `<base>/start-test-runner.sh`.
  - Windows gets a new console window.
  - Alternative: open a new terminal window per OS (Terminal via `osascript`,
    `gnome-terminal`). This is more visible but depends on which terminal is installed.
    (Draft assumes the recommendation.)
- **Q5. Releases that need new OS packages** (apt GStreamer, `playwright install-deps`):
  Recommendation: out of scope. Such a release states it in its change note, and people
  run the setup script by hand on Linux. Optional later: a `manual_setup_required` flag in
  the bundle that hides the button. (Draft: no flag.)
- **Q6. Who may update?** Recommendation: any project member, consistent with "all members
  are equal". The audit row records who. (Draft assumes this.)

## Changelog
- 2026-10-28: created. Redesigned to a side-by-side install per the user's binding
  decision. The in-place swap, supervisor/launcher, watchdog and rollback were dropped,
  together with the open questions they raised.
- 2026-10-28: user answers: Q1–Q6 the recommendations as written (mechanism: the user's side-by-side install, no rollback). Status In progress; build before feat-hitl-runner-chat.
- 2026-10-28: implemented (shared protocol/model, then parallel parts A+D, B, C, E+F, G) and reviewed in three reviewer/verifier rounds. Deviations from the plan, all from review:
  - Overall runner deadline `UPDATE_MAX_SECONDS` = 3600 (`asyncio.timeout` around the whole update; Playwright 25 min); server timeout = that + 300, refused below + 60.
  - Every runner refusal (busy, invalid, not newer, source checkout, disk space) reports `update_status failed` with the update id; `error` only when the id cannot be parsed.
  - Lost updates: the handshake carries `update_in_flight_id` (also while a final status is still unsent); settle marks the newest in-flight row succeeded (target version), keeps it (same id) or fails it ("restarted or reconnected without the update"). Status sends wait for an accepted handshake.
  - Paused help requests switch to `restore` only when an update settles succeeded, not at request time.
  - Handover marker `logs/connected-<update_id>`; the start script is written by the new runner after it connects; a runner in a source checkout (`.git`, `manage.py`, `auto_tester`) refuses to update and is never deleted.
  - Folders the updater creates carry `.punk-hazard-runner`; only marked `test-runner-*` folders count as managed (deletable). Environment cleanup protects `start-test-runner.*`.
  - `--api-key` is never carried on the new runner's command line; `install_env_file` writes it into the new `.env` (0600).
  - Archive cap 7 MiB, `MAX_MESSAGE_SIZE` in the protocol with an assert that the encoded update fits.
  - Update workflow lives in `controller_client/update_runner.py`, not `ControllerClient`.
  - Accepted residual: after a server-side expiry, a second Update pressed while the first install still runs can be marked succeeded by the first install's new runner (settle compares `to_version`; the new runner does not report which update it came from).
  - Accepted residual: if a final status is lost and a new `update_runner` is accepted before any reconnect, the older row stays in flight until it expires.
  - Status Done.
