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
  for `controller_client/`. No skipping types.
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
# anything bigger; controller screenshots / OmniParser results are several MiB,
# so always pass the two size flags (docker-compose.yml already does). Plain
# `runserver` cannot raise that limit — use it only when no controller connects.
python -m daphne -b 0.0.0.0 -p 8000 --websocket-max-message-size 33554432 --websocket-max-frame-size 33554432 auto_tester.asgi:application
python manage.py runserver              # no controller traffic only (1 MiB WebSocket cap)

# Celery workers (two separate queues)
celery -A auto_tester worker -Q upload -l info -n upload@%h
celery -A auto_tester worker -Q execution -l info -n execution@%h
celery -A auto_tester beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler

# Type checking (strict mode)
mypy .                                  # runs with strict=True, excludes venv|migrations|controller_client
controller_client/.venv/bin/mypy --config-file controller_client/setup.cfg controller_client/  # separate venv/config

# Infrastructure
docker compose up db redis searxng -d   # minimal local services
docker compose up -d                    # full stack including nginx, celery, flower
scripts/verify_server_image.sh          # build the server image and assert it can import the shared controller protocol and package the controller ZIP

# Controller client diagnostics (run on the controller machine, inside its venv,
# from the directory that contains controller_client/)
python -m controller_client.diagnose    # imports, weights, screenshot, device, model load, one inference, payload size
python -m controller_client.diagnose --browsers   # launch each browser (or --browsers firefox,msedge) and report what is missing

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

- **controller_client/** — Standalone Python client that runs on target machines. Connects to the server via WebSocket, receives action commands (click, type, screenshot, browser actions), and replies with results. Also runs OmniParser locally in-process (own venv/dependencies, own strict mypy config) to parse screenshots for element-finding — no central OmniParser service exists; the server dispatches a `find_element` action to the controller over the same WebSocket connection. The handshake carries `client_version` + a typed capability list (`controller_client/protocol.py`: `CLIENT_VERSION`, `ClientCapability`, `REQUIRED_CLIENT_CAPABILITIES`); the server rejects controllers missing a required capability before marking the project connected. After the handshake the controller preloads OmniParser in the background and reports `omniparser_status` (loading/ready/failed) which the server stores on `Project.agent_omniparser_status` and gates test-run execution on. The tracked `controller_client/` source must stay inside the server Docker image (shared protocol import + ZIP download); only its venv/weights are `.dockerignore`d. On Windows, input actions (click/hover/drag/type/key) first run `controller_client/input_guard.py`: UIPI makes Windows silently drop injected input while an elevated window (Task Manager, installers, anything past a UAC prompt) is in the foreground, and pyautogui never notices — the guard raises `InputBlockedError` instead so the agent sees a failure rather than a phantom "Clicked". Run the controller as Administrator when tests need to drive elevated apps. Optional capabilities (checked where used, not at handshake): `BROWSER_SELECTION_V1`, `DOUBLE_CLICK_V1` (`double_click`, `browser_double_click`; a case needing it on an older runner is Blocked `runner_outdated`) and `CLOSE_APP_V1` (`close_app`, `close_tab`, in `controller_client/app_closer*.py` + `app_close_policy.py`). `close_app`/`close_tab` fail closed: they never close the runner's own process, its terminal (ancestor or any known terminal host) or the OS shell, and `close_tab` refuses unless it can prove the front tab is not the runner's

### Key Patterns

**Controller Protocol**: The controller client on the target machine connects via WebSocket. The server dispatches actions (click, type, screenshot, browser commands) through the channel layer and waits for replies. All controller actions are in `projects/services.py` (functions prefixed `controller_*`).

**Authorization**: `@project_membership_required` decorator in `projects/decorators.py` handles both `@login_required` and project membership checks. Views receive a resolved `project` kwarg.

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
