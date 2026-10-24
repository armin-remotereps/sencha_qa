# Need help: pause the run for a person or a night auto-fix

Date: 2026-10-24
Plan: docs/plan/feat-human-in-the-loop.md

## What changed
- `agents/services/orchestrator*.py`, `prompt_parts.py`, `sub_agent_prompts.py`:
  - new `need_help` decision with a prompt that prefers it for environmental failures;
  - a failed recovery whose cause is not a product defect escalates to Need help;
  - the executor fails fast with an `ENVIRONMENT:` summary;
  - Continue resumes at the failing step without restore, with who fixed it, the ask, the setup agent's summary and the person's note in its state and verdict.
- `agents/types.py`, `agents/checkpoint.py`: phase `awaiting_help`, decision `need_help`, and the optional `help` key (`CheckpointHelp`). The version is unchanged.
- `projects/models.py`:
  - case status `need_help`, run status `paused` ("Needs help") and `ACTIVE_RUN_STATUSES`;
  - `BlockedReason.HELP_SKIPPED`, `RunHelpMode` and `TestRun.help_mode`;
  - `CaseHelpRequest`.
- `projects/services.py`:
  - `_pause_run_for_help` (no teardown or cleanup);
  - the PAUSED stand-down;
  - abort works on paused runs;
  - `start_test_run(help_mode=)`;
  - active-run gates use `ACTIVE_RUN_STATUSES`.
- New project modules:
  - `projects/case_help_services.py`: Continue, Mark failed, Skip, Stop auto-fix, Continue run and `finish_case_auto_fix`;
  - `case_help_state.py`, `case_help_policy.py` (Q10) and `run_help_mode_services.py` (mid-run switch);
  - `case_help_notifications.py`.
- `run_reopen_services.queue_pending_cases`. Resume and Re-run take an optional `help_mode`.
- `machine_setup`:
  - auto-fix threads (`SetupConversation.kind`, `help_request`, `fix_result`, `fix_summary`; `SetupTurn.auto_fix`);
  - `services/case_auto_fix.py`, `auto_fix_settlement.py`, `queue_turn.py` and `sweep_stale_auto_fixes.py`;
  - the `report_case_fix` tool and the thread page and WebSocket route.
- New `notifications` app: Web Push subscriptions, `send_push`, the endpoint allowlist, the Notifications page, `/sw.js` and `/manifest.webmanifest`.
- UI:
  - run page: help banner, mode in the header with a mid-run switch, mode radio at Start, mode select in Resume/Re-run;
  - case page: help card and Help history;
  - Machine setup: thread list;
  - dashboard and overview: attention items;
  - badges.

## Why
Most failing cases were environmental (installs, stuck dialogs, unclickable elements) that a person fixes in seconds. Those cases now pause the run and ask for help instead of failing. Night runs try the Machine setup agent first.

## Behaviour and contract changes
- New URLs:
  - `projects:test_run_case_help_{continue,fail,skip,stop_auto_fix}`, `projects:test_run_continue` and `projects:test_run_help_mode`;
  - `machine_setup:thread` plus the WebSocket `ws/projects/<id>/machine-setup/threads/<conversation_id>/`;
  - `notifications:{settings,subscribe,unsubscribe,test,service_worker,manifest}`.
- WebSocket run events carry `help_state` and `help_mode`.
- Migrations:
  - `projects.0027`–`0029`;
  - `machine_setup.0003`, which re-creates the one-active-conversation constraint with `kind='chat'`;
  - `notifications.0001`.
  - Rollback SQL is in the plan's Risks.
- New settings: `WEBPUSH_VAPID_PUBLIC_KEY`, `WEBPUSH_VAPID_PRIVATE_KEY`, `WEBPUSH_VAPID_SUBJECT`, `WEBPUSH_TTL_SECONDS`, `WEBPUSH_REQUEST_TIMEOUT` and `WEBPUSH_ALLOWED_ENDPOINT_HOSTS`. Generate the keys with the command in `example.env`.
- `CELERY_BEAT_SCHEDULE` gains the auto-fix sweep (every 120 s).
- New dependency `pywebpush~=2.5.0`, with the local stub `stubs/pywebpush.pyi`.
- Deploy:
  - run `migrate`;
  - start a `notify` worker (`notify-celery` in docker-compose);
  - run beat;
  - restart Daphne and every Celery worker.
- No controller protocol change.

## Notes for future changes
- Accepted risk (user decision: no extra guardrails): night auto-fix gives TestRail case text and screen text to an unattended setup agent with a shell, guarded only by "read as data".
- Q10: a step that fails again after a reported auto-fix goes straight to a person.
- A help request's text caps are not enforced by the database; services truncate.
- Push to iPhone and iPad works only from the home-screen app (iOS 16.4+).
- Not yet exercised against a live runner, a real browser push or the real model. The plan's manual Verification steps 3–15 are still to do. Verified so far with pre-commit (strict mypy), `manage.py check`, `makemigrations --check`, rolled-back smoke scripts, and three reviewer and verifier passes.
- Login throttling, which CLAUDE.md mentions, does not exist in the code. The test-push throttle uses its own cache key.
