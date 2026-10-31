# Test Runner person window v2: readable text, test case, log, Pause and discussion

Date: 2026-10-31
Plan: docs/plan/feat-runner-person-window-v2.md

## What changed
- `projects/case_help_services.py`: `continue_after_help` / `mark_failed_after_help` / `mark_blocked_after_help` take `actor: PersonStepActor`; `_resolve` writes `resolved_via` / `resolved_by_label`, credits and records only a member. `machine_setup` `send_setup_message` / `open_help_chat` take an actor; runner sends store `SetupMessage.author_label` (shown as `author_name`). Web callers pass `PersonStepActor.web(user)`.
- `controller_client/protocol.py` (CLIENT_VERSION 0.12.0): capabilities `person_case_context_v1`, `person_discussion_v1`, `case_pause_v1`; `person_step_state.cases` (`PersonCaseStatePayload`: test case text, numbered steps, log tail, pause and help/discussion parts); step entries carry `pivot_id`; messages `case_pause_action(_result)`, `person_chat_send`, `person_help_action`, `person_case_action_result`; `cut_log_tail`.
- `projects/runner_case_services.py` (cases: step, waiting help, running; one query, log tail cut in SQL), `projects/runner_help_action_services.py` (`resolve_help_from_runner`, stale-card guard `find_runner_help_case`), `machine_setup/services/runner_discussion.py` (newest 40 rows, tool rows as notices, side-effect-free send gate; `send_from_runner` via `open_help_chat`, 10/min), `projects/runner_window_state.py` (composition).
- `projects/controller_consumer.py`: v2 snapshot, nudges coalesced to one push per second with a push lock, handlers for the three new messages (forms `RunnerCasePauseForm`, `RunnerChatSendForm`, `RunnerHelpActionForm`). `machine_setup/events.py` message / turn / help broadcasts nudge the runner (top-level rows only).
- `machine_setup/services/send_gates.thread_send_refusal`, `runner_person_step_services.hit_runner_throttle`: shared gate and throttle helpers.
- `open_paused_help_chat`: a Test Runner pause message is sent as "<member> via Test Runner" or "a person at the Test Runner".
- Window (`controller_client/person_window_app.py`, `person_window.py`, `person_window_ipc.py`, `client.py`, `config.py`, `main.py`, diagnostics): header with Pause… and A−/A+, Notebook tabs Step (one-tap Same as expected / Different…), Test case (covered steps tinted), Log, Discussion (chat, proposal and plan draft cards, Continue with mode, Mark failed / blocked, Open on Punk Hazard); every text a read-only copyable block; `CONTROLLER_PERSON_WINDOW_TEXT_SIZE`.

## Why
Testers found the Tk window small, uncopyable and missing what the web case page offers; a web console was rejected because only the runner can take screenshots.

## Behaviour and contract changes
- Migrations `projects.0052_help_resolved_via`, `machine_setup.0010_message_author_label` (additive).
- Controller protocol 0.12.0 (above). Older runners get no `cases` and are refused the new messages; their window is unchanged.
- Runner `.env`: `CONTROLLER_PERSON_WINDOW_TEXT_SIZE` (default 14, 10–28; commented in the download `.env`).
- The help history shows `resolved_by_label`. Anonymous Test Runner pauses now auto-send their message.
- Deploy: `migrate`, rebuild the server image, restart Daphne/web and every Celery worker, update runners from the Environment page.

## Notes for future changes
- The window shows Need help cases of every run mode while the request waits for a person and no Night auto-fix runs or starts (plan Q1).
- A snapshot with a waiting help case costs about 20 queries (help panel, wake and send gates); 1–2 otherwise. Only pushed on nudges, coalesced and deduplicated.
- The window always sends the shown draft's id with Continue; the server supersedes only that draft.
- Runner labels are `PersonStepActor.label()` and may show a member's email (Connection-key trust model).
- Verified: pre-commit (strict mypy server + controller incl. win32), `manage.py check`, `makemigrations --check`, `sqlmigrate` for both migrations, a throwaway-DB smoke (query counts, log never loaded, 40-row discussion, anonymous Mark failed), `diagnose --window --no-interactive` with real Tk (widget state, no screenshots), two reviewer/verifier rounds. Not run against a live runner, on Windows/Linux Tk, or in a browser.
