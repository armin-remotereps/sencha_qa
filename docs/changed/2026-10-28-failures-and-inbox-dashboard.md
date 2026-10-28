# Failures page, attention inbox, slimmer dashboard, run finished push, machine health

Date: 2026-10-28
Plan: docs/plan/feat-failures-and-inbox-dashboard.md

## What changed
- `notifications`: the per-user inbox.
  - `MissedFailure` (one row per user and run case) and `MissedReply` (one row per user and thread). An upsert re-opens a dismissed row.
  - Written by `record_missed_failure` from `notify_case_failed` (agent verdict only) and by `record_missed_reply` from `notify_turn_reply`, before the ntfy checks, so the inbox works with ntfy off.
  - Read by `services/inbox.py` (`get_inbox`, `count_inbox`). Only member, non-archived projects; a failure only while the case is Failed; a reply only while the thread is not archived.
  - Dismissed by `services/dismiss_inbox.py`: one item, or all up to the page-render time.
  - `services/inbox_broadcast.py` sends payload-free `attention.changed` events to `attention_user_<id>` groups.
- Help needs are not stored: they are read live from open `CaseHelpRequest` rows ("Auto-fix is trying" while the Night auto-fix runs). Every open, resolve, auto-fix end and stop broadcasts.
- `dashboard`:
  - `services.py` became a `services/` package.
  - The home keeps only "Needs your attention" (Needs help / Missed failures / Missed replies) and a Failed cases widget (last 7 days).
  - Run summary, recent projects, recent activity, the disconnected-machine item and `partials/stat_card.html` are deleted.
- Unread badge: the `attention_badge` context processor shows it on the Dashboard nav link, the mobile Menu button and a `(N) ` tab-title prefix.
- Live updates: `AttentionConsumer` (`ws/attention/`) recounts at most every 2 s, and `attentionLive` swaps `#attention-card` and `#failed-widget` from `GET /attention/`.
- `/failures/` (`dashboard/failure_views.py`):
  - filters: date range, project, run, title or TestRail id, review state, failed by; paginated;
  - a "By test case" grouped view;
  - saved presets (`FailedCaseFilterPreset`, at most 20 per user);
  - streamed CSV export: async keyset pages, at most 50,000 rows, formula-safe cells, 5 exports per minute per user;
  - bulk Check / Re-run (cases of one run) / Send to TestRail, through the existing per-run services.
- `TestRunTestCase.failed_by` (agent / help / review) is set by the agent verdict, Mark failed after help and an applied result change. It is shown and filterable.
- `/insights/machines/`: help requests by fault per day, the Night auto-fix success rate, and how help requests were resolved.
- Run finished:
  - `projects.services.finish_test_run` is now the only place that sets a run to DONE. It is a conditional UPDATE on status and generation, used by case workers and by Mark failed / Mark blocked.
  - The winning caller sends `notify_run_finished` (counts and duration) and an attention broadcast on commit.
- Night digest: the beat task `notifications.tasks.send_night_digest` (`notify` queue, at `NIGHT_DIGEST_HOUR` UTC) sends one push per user covering the Night runs of the last `NIGHT_DIGEST_LOOKBACK_HOURS`. Each run is claimed once via `TestRun.night_digest_sent_at`.
- `auto_tester/pagination.py` and `csv_safe.py`: shared helpers. `machine_setup/services/conversation_url.py` is the single home for thread links.

## Why
The dashboard's other cards were not used. People missed failures and chat replies that only came as ntfy pushes. Failed cases had no cross-project view, and nothing reported a finished run.

## Behaviour and contract changes
- New URLs:
  - `dashboard:{attention_fragment,failed_cases,failed_cases_export,failed_cases_bulk,preset_save,preset_delete,machine_health}`;
  - `notifications:{dismiss_failure,dismiss_reply,dismiss_all}` (POST).
- New WebSocket route: `ws/attention/`, server to client `{"type":"attention","count":N}`.
- New notification kinds `RUN_FINISHED` and `NIGHT_DIGEST`, with checkboxes on `/notifications/`, both on by default.
- Migrations:
  - `notifications.0004`;
  - `projects.0034` (`failed_by`, `night_digest_sent_at`);
  - `projects.0035` (concurrent `(status, -finished_at)` index, non-atomic);
  - `dashboard.0001`.
- Settings: `NIGHT_DIGEST_HOUR` (default 7) and `NIGHT_DIGEST_LOOKBACK_HOURS` (default 24); a beat entry and a context processor.
- Deploy: run `migrate`, then restart Daphne, every Celery worker and beat (beat installs the new schedule).

## Notes for future changes
- Inbox rows are written regardless of the ntfy on/off switch. The checkboxes only control pushes.
- `failed_by` is blank ("Not recorded") for rows from before this change. There is no backfill.
- Bulk Send pushes every checked, unsent case of each affected run, not only the selected ones (the dialog says so).
- `stubs/channels/db.pyi` is a local stub for `channels.db`.
- Not changed: the run page's case Duration cell only updates on reload; `templates/partials/pagination.html` still has an inline `onchange` (pre-existing).
- Not exercised against a migrated database, a live socket, a real ntfy push or beat. Smoke-checked on throwaway test databases: pages, query counts, CSV paging, throttle and consumer throttling. Also checked with strict mypy, `manage.py check`, `makemigrations --check`, and three reviewer and verifier passes.
