# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Sencha QA (Auto Tester) — an AI-powered QA automation platform. Users create projects, import test cases from TestRail (a whole suite or a single run) and run them through an AI agent that controls a remote machine (via a WebSocket-connected controller client) to execute tests against real applications.

## Session start

At the beginning of every session, before doing anything else, read the notes in
`docs/changed/` (newest first by filename date) to learn what has changed recently
and why. They are short by design; read them all, or at least those newer than the
last commit you already know about. Also skim `docs/plan/` for plans whose Status
is not `Done`, since those describe work in flight. Older history lives in
`docs/specs/` (requests) and `docs/impl/` (implementation notes and plans).

## Workflow

Every code change follows the loop in `.claude/rules/workflow.md`:
plan (`planner` agent, `docs/plan/`) -> implement (independent parts in
different files go to parallel sub-agents, then the orchestrator integrates and
does a final pass) -> `reviewer` + `verifier` in parallel -> fix -> repeat review
until clean -> document in `docs/changed/`.

## Conventions

- One Django app per domain. Business logic goes in the app's service layer; new
  use cases go in `<app>/services/<use_case>.py` (as in `agents/services/`).
  `accounts/services.py`, `projects/services.py` and `dashboard/services.py` are
  legacy single-module services: do not add unrelated use cases to them.
- Views, WebSocket consumers and Celery tasks are thin: validate input, call a
  service, map domain errors to responses. They never touch the ORM directly and
  hold no business logic.
- No REST API: server-rendered HTML with the Django template engine, data passed
  via template context, input through Django forms. Alpine.js only for sockets and
  small UI behaviour (dialogs, tabs, hamburger menu, ...).
- Domain errors are plain exception classes in `<app>/exceptions.py`.
- Security: every sensitive view requires authentication; project views use
  `@project_membership_required` for authorization; login and other credential
  surfaces are throttled; no XSS (`|safe`/`mark_safe` on user data), SQL, shell or
  other injection.
- Custom user model (`accounts`); keep it extensible.
- All imports at module top level; no lazy imports. Full type annotations
  everywhere; mypy runs in strict mode for the server and, with its own config,
  for `controller_client/` and `vision_worker/`. No skipping types.
- Settings: every env var is read in `settings.py` via `python-decouple` (never
  import `decouple` elsewhere, never read `os.environ` in app code). All service
  configs (db, redis, LLM, ...) come from `.env`; keep `example.env` up to date.
  `STATIC_URL`/`STATIC_ROOT` and `MEDIA_URL`/`MEDIA_ROOT` are both defined.
- Celery uses redis as broker, `django-celery-results` as result backend and
  `django-celery-beat` for schedules.
- This project does not contain test cases. Do not add `tests.py`, `tests/`
  packages, or test tooling; verify changes with the type checker and manual
  smoke checks instead.

## Commands

```bash
# Dev server (ASGI via Daphne, required for WebSockets). Daphne caps incoming
# WebSocket messages at 1 MiB by default and closes the socket (code 1009) on
# anything bigger; controller screenshots / vision worker parse results are several MiB,
# so always pass the two size flags (docker-compose.yml already does). Plain
# `runserver` cannot raise that limit — use it only when no controller connects.
python -m daphne -b 0.0.0.0 -p 8000 --websocket-max-message-size 33554432 --websocket-max-frame-size 33554432 auto_tester.asgi:application
python manage.py runserver              # no controller traffic only (1 MiB WebSocket cap)

# Celery workers (three separate queues)
celery -A auto_tester worker -Q upload -l info -n upload@%h
celery -A auto_tester worker -Q execution -l info -n execution@%h
celery -A auto_tester worker -Q notify -l info -n notify@%h
celery -A auto_tester beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler

# Type checking (strict mode)
mypy .                                  # runs with strict=True, excludes venv|migrations|controller_client|vision_worker
controller_client/.venv/bin/mypy --config-file controller_client/setup.cfg controller_client/  # separate venv/config
vision_worker/.venv/bin/mypy --config-file vision_worker/setup.cfg vision_worker/              # separate venv/config

# Infrastructure
docker compose up db redis searxng -d   # minimal local services
docker compose up -d                    # full stack including nginx, celery, flower
scripts/verify_server_image.sh          # build the server image and assert it can import the shared controller protocol and package the controller ZIP

# Controller client diagnostics (run on the controller machine, inside its venv,
# from the directory that contains controller_client/)
python -m controller_client.diagnose    # screen capture
python -m controller_client.diagnose --browsers   # launch each browser (or --browsers firefox,msedge) and report what is missing
python -m controller_client.diagnose --input      # screen scale vs OS, pointer mapping, scroll direction, stuck modifiers; prints a summary block to paste back (--no-interactive: automatic checks only)

# Vision worker (on the GPU machine, inside vision_worker/.venv, from the repo root; see vision_worker/README.md)
python -m vision_worker.main            # connect to VISION_SERVER_URL and serve parse requests
python -m vision_worker.diagnose        # imports, weights, device, model load, one inference, result size

# Formatting and typing — same checks pre-commit runs
pre-commit run --all-files

# Migrations — review models first and warn about any data loss before migrating
python manage.py makemigrations
python manage.py migrate

# Dependencies — NEVER use pip freeze. Find on PyPI, add to requirements.txt as x~=1.0.1, then:
pip install -r requirements.txt
```

## Architecture

### controller_client

- **controller_client/** — Standalone Python client that runs on target machines. Connects to the server via WebSocket, receives action commands (click, type, screenshot, browser actions), and replies with results. Own venv/dependencies and own strict mypy config; it no longer runs OmniParser (see vision_worker below). The handshake carries `client_version` + a typed capability list (`controller_client/protocol.py`: `CLIENT_VERSION`, `ClientCapability`, `REQUIRED_CLIENT_CAPABILITIES`); the server rejects controllers missing a required capability before marking the project connected. The tracked `controller_client/` source must stay inside the server Docker image (shared protocol import + ZIP download); only its venv is `.dockerignore`d. On Windows, input actions (click/hover/drag/scroll/type/key) first run `controller_client/input_guard.py`: UIPI makes Windows silently drop injected input while an elevated window (Task Manager, installers, anything past a UAC prompt) is in the foreground, and pyautogui never notices — the guard raises `InputBlockedError` instead so the agent sees a failure rather than a phantom "Clicked". Run the controller as Administrator when tests need to drive elevated apps. Optional capabilities (checked where used, not at handshake): `BROWSER_SELECTION_V1`, `DOUBLE_CLICK_V1` (`double_click`, `browser_double_click`; a case needing it on an older runner asks for help with the fault `runner_outdated`), `CLOSE_APP_V1` (`close_app`, `close_tab`, in `controller_client/app_closer*.py` + `app_close_policy.py`), `WINDOWS_SHELL_V1` and `SCROLL_V1` (`scroll`; an older runner gets a tool error with a key_press fallback, not a help request). `close_app`/`close_tab` fail closed: they never close the runner's own process, its terminal (ancestor or any known terminal host) or the OS shell, and `close_tab` refuses unless it can prove the front tab is not the runner's. Every pointer action maps screenshot pixels to the input backend's coordinates in `controller_client/executor.py` (`pointer_mapping.map_to_input`, per-axis scale from `capture_size()`/`input_size()`; equal sizes change nothing), so the server always sends screenshot-pixel coordinates

### vision_worker and visual_engine

- **vision_worker/** — Standalone GPU client running OmniParser (vendored in `vision_worker/omniparser/`, weights `.gitignore`d), own venv/requirements/strict mypy config. Set up with `vision_worker/scripts/setup.sh` / `setup.ps1` and run with `python -m vision_worker.main`, like the Test Runner; one process per GPU (`CUDA_VISIBLE_DEVICES`). No Docker. Connects out over wss to `ws/vision-worker/` with the shared `VISION_WORKER_TOKEN`, preloads the model, reports `status` (loading/ready/failed; also a 30 s heartbeat) and answers `parse_request` with elements + an annotated image; it answers `BUSY` beyond 1 running + `VISION_MAX_QUEUE` queued. Wire protocol in `vision_worker/protocol.py` (stdlib only; the server imports it, so the source stays in the server image).
- **visual_engine** (Django app) — global, not per project. `VisionWorker` rows (one per worker name; newest connection wins, stale after `VISION_WORKER_STALE_SECONDS` without a heartbeat), `VisionWorkerConsumer`, and services: `parse_screenshot` rotates over ready workers round robin with a Redis counter (shared by all Celery processes), skips busy ones and retries disconnects within `VISION_PARSE_BUDGET_SECONDS`. `projects.services.controller_find_elements` = runner `controller_screenshot` + `parse_screenshot`. Run/resume/re-run/machine-setup gating and the machine badge need at least one ready worker (`MachineState.VISUAL_ENGINE_OFFLINE` when none is connected).

### Need help (human in the loop)

- When a step fails for an environmental reason, the orchestrator decides `need_help` instead of failing. The case goes **Need help** (`need_help`), the run **Needs help** (`TestRunStatus.PAUSED`, in `ACTIVE_RUN_STATUSES`), and a `CaseHelpRequest` is opened. The worker exits; the chain's remaining tasks stand down on PAUSED. No teardown or cleanup, so the machine stays as it was.
- Per-run `help_mode`: **Attended** notifies project members (ntfy) right away; **Night** first opens a Machine setup auto-fix thread (`SetupConversation.kind=auto_fix`, `report_case_fix` tool, existing limits only), then falls back to a person if not fixed. One auto-fix per step per from-scratch execution (`projects/case_help_policy.py`). The mode can be switched mid-run (`projects/run_help_mode_services.py`).
- Only a person marks a case Blocked: Mark blocked (always `help_skipped`, the only literal Blocked write) or an applied result change to Blocked (`person_review`, assigned from a variable). Infrastructure faults (runner lost or unresponsive, visual engine, AI service, time limit, browser unavailable, runner outdated, resume state lost, internal error) never block a case: the worker opens a help request with `CaseHelpRequest.fault` (`projects/case_faults.py`), and Night mode lets the auto-fix agent decide whether it can fix it.
- Continue / Mark failed / Mark blocked / Stop auto-fix in `projects/case_help_services.py`. Continue follows `CaseHelpRequest.continue_mode`, decided when the case paused (`projects.case_faults.help_continue_mode`): `current_screen` writes the checkpoint `help` key with `continue_pending` (re-run the failing step, no cleanup or restore), `restore` writes it without (cleanup and restore sub-task first; runner faults, or any fault before a due restore finished: `restore_baseline` in `execute_test_run_test_case`), `from_scratch` clears the checkpoint (no checkpoint was saved yet). The setup agent's and the person's notes reach the case's state except from scratch.
- **Help chat** (`docs/changed/2026-10-24-help-chat.md`): from a Need help case a person opens a multi-turn chat (`machine_setup:help_chat_open` → a `SetupConversation` thread of kind `case_help`, or the request's existing auto-fix thread; one thread per help request). Person turns there get the `CASE_HELP` role (`machine_setup/thread_role.py`): `run_setup_task` (full machine) plus `get_help_case`, `propose_plan_change` and `propose_resolution`. A redesign is a whole new plan whose leading steps may be *carried* from earlier steps that passed (`agents/plan_revision.py` validates: strictly increasing prefix, each once, at least one step runs); it is a `CasePlanRevision` draft until the person presses Continue with "Use the redesigned plan" ticked, then the case resumes at the first step not carried, with no cleanup or restore. The agent's suggestion is shown only, never pre-filled or sent to the tester. The Night auto-fix turn has no plan tools.
- **Result chat** (`docs/changed/2026-10-26-case-result-chat.md`): from a Passed / Failed / Blocked case a person opens a multi-turn chat (`machine_setup:result_chat_open` → one non-archived `SetupConversation` of kind `case_result` per case, `result_case` FK). Its turns get the `CASE_RESULT` role: `run_setup_task` (full machine) plus `get_case_result`, `read_case_log`, `ask_case_screenshot`, `propose_result_change` and `propose_plan_change`. Proposals are cards (`CaseResultChange`, or `CasePlanRevision` with `source=review` and no help request); only a person's Apply (`projects/case_result_change_services.py`: new status + server header + summary, Checked/Sent cleared) or "Re-run with redesigned plan" (`projects/case_review_redesign_services.py`: Resume path via `run_reopen_services`, cleanup + restore of carried steps, then step C+1) acts. Sends are refused while a run is Started (not Paused) or the machine is not READY. Stale cards are refused by fingerprints.
- `projects.services` must never import `machine_setup.services` (cycle): use the leaf modules `machine_setup.task_names` / `auto_fix_stop` / `turn_state` and `send_task` by name. A beat sweep (`machine_setup.tasks.sweep_stale_auto_fixes`, `notify` queue) settles lost or dead auto-fixes.
- **Human in the loop** run mode (`RunHelpMode.HUMAN_IN_THE_LOOP`, stored `"hitl"`; `docs/changed/2026-10-28-hitl-run-mode.md`): a person performs every step. The agent plans, prepares each step for a person (`agents/services/person_steps.py`; a command only when the case states it as a whole, shell label from the runner's OS via `agents/os_shell.py`), the case goes `awaiting_person`, the run PAUSED (`RunHelpState.PERSON_STEP`), and a `CasePersonStep` row is opened. The person reports from the "Your turn" card (`projects/person_step_services.py`: report, overrule, mark failed, mark blocked); the re-queued worker confirms with a sub-agent limited to `take_screenshot` / `execute_command` / `wait_for_command` (read-only by prompt), records a `performed_by="person"` attempt and hands out the next step. Not confirmed → back to the person; "different" → step failed, case stops. The checkpoint `person` key carries the hand-off and report; rows are settled from the checkpoint save hook (`projects/person_step_rows.py`). No AI teardown, no Night auto-fix; faults still open Need help. Switching away hands a waiting step back to the agent (current screen). Resume, help Continue (except current screen) and plan redesigns drop the `person` key.
- **notifications** app: ntfy. Each user has one `NotificationPreference` (server-generated random topic, on/off, kinds Need help / Failed) managed on `/notifications/`. `send_notification` (on the `notify` queue) publishes JSON to `NTFY_SERVER_URL` with `httpx`, with an optional `NTFY_ACCESS_TOKEN`; an empty server URL disables it. Click links need `SITE_URL`. Topics act like passwords: never log them or put them in task args. Entry points: `projects.case_help_notifications.notify_case_needs_help` and `projects.case_failed_notifications.notify_case_failed` (agent verdict Failed only).

### Key Patterns

**Controller Protocol**: The controller client on the target machine connects via WebSocket. The server dispatches actions (click, type, screenshot, browser commands) through the channel layer and waits for replies. All controller actions are in `projects/services.py` (functions prefixed `controller_*`).

**Authorization**: `@project_membership_required` decorator in `projects/decorators.py` handles both `@login_required` and project membership checks. Views receive a resolved `project` kwarg. All members are equal (no owner or roles); members invite others by email and leave on the Settings tab (`projects/project_member_services.py`), and a project always keeps at least one active member.

### Frontend

- User-facing brand is **Punk Hazard** (internal package names stay `auto_tester`). Brand assets in `static/branding/`; `brand_name`/`brand_tagline` come from `auto_tester/context_processors.py`
- Own design system, no Tailwind: `static/css/punk-hazard.css` (dark-first tokens + component classes — `.btn-*`, `.badge-*`, `.card`, `.data-table` with `data-label` mobile records, `.dialog-backdrop`, `.steps`, `.checklist`, …) ported from the approved `ui-prototype/`. Add classes there rather than inline styles
- Alpine.js 3.14 (pinned CDN, plus `@alpinejs/focus`) for dialogs, tabs, bulk selection and WebSocket components; shared `Alpine.data` components (`dialog`, `osTabs`, `bulkSelect`, `clipboard`, `mobileNav`, `dismissible`, `rowMenu`) and `punkHazard.wsUrl()` live in `static/js/punk-hazard.js`
- Templates: `templates/base.html` → `templates/layouts/{app,auth,project}.html`; reusable includes in `templates/partials/` (contracts + dialog convention documented in `templates/partials/README.md`); page-private fragments in `templates/<app>/includes/`. Django `{# #}` comments are single-line — use `{% comment %}` for blocks
- User-facing terminology: Test Runner (controller client), test machine, Visual engine (OmniParser, advanced diagnostics only), Application context (project prompt), Runs / Draft / Running / Passed, Import from TestRail, Connection key (API key)
- Onboarding wizard (`projects:create` → `setup_*` views) is not resumable and stores no wizard state; every step reads real project state
- No REST API — server-rendered HTML, data passed via template context

### Settings & Configuration

- All env vars read via `python-decouple` in `settings.py` — never import `decouple` directly elsewhere

## Parallel Development

This project uses git worktrees for parallel development. Each worktree must use a unique port when running the Django dev server to avoid conflicts (e.g., `runserver 8001`, `daphne -p 8001`).
