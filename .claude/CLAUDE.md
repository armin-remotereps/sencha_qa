# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Sencha QA (Auto Tester) — an AI-powered QA automation platform. Users create projects, upload TestRail XML test cases, and run them through an AI agent that controls a remote machine (via a WebSocket-connected controller client) to execute tests against real applications.

## Commands

```bash
# Dev server (ASGI via Daphne, required for WebSockets)
python manage.py runserver              # standard Django dev server
python -m daphne -b 0.0.0.0 -p 8000 auto_tester.asgi:application  # production-like

# Celery workers (two separate queues)
celery -A auto_tester worker -Q upload -l info -n upload@%h
celery -A auto_tester worker -Q execution -l info -n execution@%h
celery -A auto_tester beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler

# Type checking (strict mode)
mypy .                                  # runs with strict=True, excludes venv|migrations|controller_client

# Formatting
black .
isort .

# Infrastructure
docker compose up db redis searxng -d   # minimal local services
docker compose up -d                    # full stack including nginx, celery, flower

# Migrations (use /migrate skill for the full workflow)
python manage.py makemigrations
python manage.py migrate

# Dependencies — NEVER use pip freeze. Find on PyPI, add to requirements.txt as x~=1.0.1, then:
pip install -r requirements.txt
```

## Architecture

### Django Apps

- **auto_tester/** — Django project config (settings, celery, asgi, urls)
- **accounts/** — Custom user model (`CustomUser`, email-based auth, no username), login/logout views, `EmailBackend`
- **projects/** — Core domain: Projects, TestCases, TestCaseUploads, TestRuns, TestRunTestCases, TestRunScreenshots. Contains views, services, tasks, models, consumers, forms, controller protocol
- **agents/** — AI agent system: agent loop, tool definitions, tool registry, LLM client, vision QA, OmniParser integration, context/output summarizers, search tools
- **dashboard/** — Landing/home page
- **controller_client/** — Standalone Python client that runs on target machines. Connects to the server via WebSocket, receives action commands (click, type, screenshot, browser actions), and replies with results. Also runs OmniParser locally in-process (own venv/dependencies, own strict mypy config) to parse screenshots for element-finding — no central OmniParser service exists; the server dispatches a `find_element` action to the controller over the same WebSocket connection

### Key Patterns

**Service Layer**: All business logic lives in `services.py` files. Views and Celery tasks are thin — they call service functions, never implement logic directly.

**Celery Queues**: Two dedicated queues:
- `upload` — XML file parsing (`process_xml_upload`)
- `execution` — AI test execution (`execute_test_run_case`)

Task routing is defined in `settings.CELERY_TASK_ROUTES`.

**WebSocket Consumers** (Django Channels): Real-time updates via `projects/consumers.py` and `projects/routing.py`:
- Upload progress (`ws/projects/<id>/uploads/`)
- Test run status (`ws/projects/<id>/test-runs/<id>/`)
- Test case live logs/screenshots (`ws/projects/<id>/test-runs/cases/<id>/`)
- Agent connection status (`ws/projects/<id>/agent-status/`)
- Controller protocol (`ws/controller/`) — agent machine connects here

**Controller Protocol**: The controller client on the target machine connects via WebSocket. The server dispatches actions (click, type, screenshot, browser commands) through the channel layer and waits for replies. All controller actions are in `projects/services.py` (functions prefixed `controller_*`).

**AI Agent Loop** (`agents/services/agent_loop.py`): Observe-think-act loop that uses OpenAI for reasoning and vision. Tools are registered via `tool_registry.py` and defined in `tool_definitions.py`. The agent takes screenshots, analyzes them, and executes actions on the remote machine.

**Authorization**: `@project_membership_required` decorator in `projects/decorators.py` handles both `@login_required` and project membership checks. Views receive a resolved `project` kwarg.

### Frontend

- Django templates with Tailwind CSS (CDN) and Alpine.js (CDN)
- Templates in `templates/` directory: `base.html` at root, app-specific templates in subdirectories
- Alpine.js for WebSocket connections, dynamic UI interactions
- No REST API — server-rendered HTML, data passed via template context

### Settings & Configuration

- All env vars read via `python-decouple` in `settings.py` — never import `decouple` directly elsewhere
- `.env` file at project root (see `example.env` for all variables)
- Custom stubs in `stubs/` directory for mypy
- `setup.cfg` contains mypy, isort, and pytest configuration

### Database

- PostgreSQL 17 (`psycopg` driver)
- Redis for cache, Celery broker, and Django Channels layer

### Models Hierarchy

```
Project → TestCaseUpload → TestCase
Project → TestRun → TestRunTestCase (pivot) → TestRunScreenshot
Project has: members (M2M CustomUser), tags (M2M Tag), api_key, agent_connected
```

## Testing

Verify new logic with unit tests (pytest / Django's test framework) rather than standing up the real running system. The user runs end-to-end/manual verification themselves — don't dispatch `nightmare-tester`/`logic-tester` against the live system unless explicitly asked.

## Parallel Development

This project uses git worktrees for parallel development. Each worktree must use a unique port when running the Django dev server to avoid conflicts (e.g., `runserver 8001`, `daphne -p 8001`).

## Spec-Driven Development

Feature specs live in `docs/specs/`, implementation docs in `docs/impl/`. When implementing a spec (e.g., `docs/specs/001.User Management.md`), create corresponding `docs/impl/001.User Management-impl.md` after completion.
