# User leaderboard

Date: 2026-10-30
Plan: docs/plan/feat-user-leaderboard.md

## What changed
- `projects/models.py`: `TestRunTestCase.credited_user` (nullable FK, no index): the person who last drove the case. Indexes on `CaseHelpRequest.resolved_at` and `CasePersonStep.reported_at`.
- `projects/case_credit.py` (new, leaf): `credit_cases` and `CREDITED_HELP_RESOLUTIONS`. Called next to `record_case_activity` at Start, Resume, Re-run, redesign re-run, help Continue / Mark failed / Mark blocked (`case_help_services._resolve`) and person steps reported by a known user (`person_step_services._after_action`).
- `dashboard/leaderboard_types.py`, `leaderboard_rules.py` (windows, points, ranking, movement, streak, badges; pure), `services/leaderboard.py` (`get_leaderboard`, 8 queries for 7 days, 7 for Today, constant), `leaderboard_forms.py`, `leaderboard_views.py`.
- Page `/leaderboard/` (`dashboard:leaderboard`): Today / Last 7 days tabs, project and run-mode filters, podium, standings table, "Your position", How points work. Templates `dashboard/leaderboard.html` + `includes/_leaderboard_{podium,table,move,medal}.html`; `lb-` classes in `punk-hazard.css`.
- Nav: the Insights entry is now "★ Leaderboard"; Machine health is linked from the leaderboard header and still highlights it.

## Why
The Insights (machine health) page was not useful. Testers want a fun, competitive view of how each user is doing (Passed / Failed / Blocked / Needs re-test, tests remaining), visible to everyone.

## Behaviour and contract changes
- New GET URL `/leaderboard/?period=today|7d&project=&mode=`, any logged-in user. Invalid values fall back to 7 days / all projects / all modes; the project choices are the viewer's own projects.
- Points: Passed 2, Failed 2, Blocked 1, Needs re-test 0, person step reported 1, help resolved 1. Results count by current status and `finished_at` in the window. Today starts 00:00 `TIME_ZONE` (UTC); movement compares with the same span just before.
- Visibility is global: every user sees every active writable member (plus anyone credited in the window), with counts across all projects; no foreign project names. Users without a full name show by email.
- Remaining: cases without a verdict in Draft, Running and Needs-help runs of each user's non-archived writable projects (snapshot).
- Migrations: `projects.0046_run_case_credited_user` (ADD COLUMN), `0047_backfill_case_credit` (best effort, last 14 days + open cases, from resume / help / person step / session records; `atomic=False`), `0048_leaderboard_window_indexes` (concurrent, `atomic=False`; if it fails, drop the INVALID index and rerun).
- Deploy: `migrate`, then restart web and every Celery worker together (an old process would start runs without setting credit).

## Notes for future changes
- A new action that makes a case execute or settles it must call `credit_cases`. Reviews (re-test marks, Undo, applied result changes, Checked, TestRail sends), Night auto-fix, anonymous Test Runner answers and Reset never move credit.
- A re-run replaces the verdict, so only the latest one counts (no point farming). Runs started before the deploy and outside a test session stay unattributed; the page shows that count, together with results of deactivated users.
- Verified with pre-commit (strict mypy for all configs), `makemigrations --check`, migrations forward/back/forward on a throwaway DB, a rolled-back smoke script (26 checks: counts, windows, movement, filters, query counts, escaping, foreign project names, GET-only, login), a browser check on desktop and mobile, and two reviewer/verifier rounds. Not exercised with a live run on a Test Runner.
- The shared `.tabs` style shows a 1px vertical scrollbar (its `-1px` margin plus `overflow-x: auto`). This was already there on project tabs and Failures and is not fixed here.
