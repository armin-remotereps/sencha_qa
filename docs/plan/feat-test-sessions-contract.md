# Test sessions: implementation contract (shared by the sub-agents)

Plan: `docs/plan/feat-test-sessions.md`. Already committed: `test_sessions/models.py`
(`TestSession`, `TestSessionCase`), `case_activity.py` (`CaseActivity`,
`VERDICT_DISCARDING`), `exceptions.py`, `admin.py`, migration `0001_initial`.
User decisions: all four kinds of activity are recorded explicitly; auto-close after
`TEST_SESSION_MAX_HOURS` (default 12, 0 = off); owner-only pages, read-only admin for
superusers; both hours shown (wall clock + test time); a case counts once per session.

Import rule: `test_sessions` modules may import `projects.models` (and `accounts`,
`auto_tester` helpers) but never `projects.services` or any other `projects` module.

## Part A: `test_sessions/services/record_case_activity.py`

```python
def record_case_activity(*, user: CustomUser | None, pivot_ids: Sequence[int], activity: CaseActivity) -> None
def freeze_session_cases_of_run(test_run_id: int) -> None
def freeze_finished_cases_of_session(session_id: int, now: datetime) -> None
```
All three run in a savepoint and swallow (and log) `DatabaseError` only.
`freeze_finished_cases_of_session` is used by Stop and by auto-close.

## Part C: session services (`test_sessions/services/`)

```python
# start_session.py
def start_session(*, user: CustomUser, name: str, note: str) -> TestSession  # SessionAlreadyOpenError
# stop_session.py
def stop_session(*, user: CustomUser, session_id: int, note: str | None) -> TestSession  # SessionNotFoundError, SessionAlreadyClosedError; note None = keep
# edit_session.py
def edit_session(*, user: CustomUser, session_id: int, name: str, note: str) -> TestSession
# delete_session.py
def delete_session(*, user: CustomUser, session_id: int) -> None
# close_stale_sessions.py
def close_stale_sessions(now: datetime) -> int
# export_throttle.py
def check_session_export_throttle(user: CustomUser) -> None  # SessionExportThrottledError
# session_export.py
async def stream_session_csv(session_id: int) -> AsyncIterator[str]
# session_queries.py
def get_open_session(user: CustomUser) -> TestSession | None
def get_session_for_user(user: CustomUser, session_id: int) -> TestSession  # SessionNotFoundError
def list_sessions(user: CustomUser, *, page: int, per_page: int) -> Page[SessionListRow]
def get_session_summary(session: TestSession) -> SessionSummary
def list_session_cases(session: TestSession, user: CustomUser, *, page: int, per_page: int) -> Page[SessionCaseRow]
```

Row types (frozen dataclasses in `session_queries.py`):

```python
SessionListRow(id: int, display_name: str, started_at: datetime, ended_at: datetime | None,
               is_open: bool, closed_automatically: bool, wall_clock: timedelta,
               test_count: int, project_count: int, test_time: timedelta | None)
StatusCount(status: str, label: str, count: int)
ProjectBreakdown(project_id: int | None, project_name: str, test_count: int, test_time: timedelta | None)
SessionSummary(wall_clock: timedelta, test_time: timedelta | None, test_count: int,
               project_count: int, person_step_count: int,
               status_counts: list[StatusCount], by_project: list[ProjectBreakdown])
SessionCaseRow(id: int, pivot_id: int | None, project_id: int | None, project_name: str,
               run_id: int | None, run_name: str, case_title: str, testrail_id: str,
               status: str, status_label: str,  # status "" = not available
               failed_by_label: str, blocked_reason_label: str,
               started_at: datetime | None, finished_at: datetime | None, duration: timedelta | None,
               performed_by_label: str, actions_label: str, person_step_count: int,
               is_live: bool, case_url: str | None)
```

Settings (part C owns every `auto_tester/settings.py` edit from now on):
`TEST_SESSION_MAX_HOURS`, the context processor
`"test_sessions.context_processors.open_test_session"` (written by part E), the
Celery route of `test_sessions.tasks.close_stale_test_sessions` to `notify` and the
beat entry `test-sessions-close-stale`.

## Part E: web

- `test_sessions/context_processors.py`: `open_test_session(request) -> dict[str, object]`,
  key `open_test_session` (a `SimpleLazyObject` of `get_open_session`, or None when
  anonymous).
- URL names (`app_name = "test_sessions"`, included at the root in `auto_tester/urls.py`):
  `list` `test-sessions/`, `start` `test-sessions/start/`, `detail`
  `test-sessions/<int:session_id>/`, `stop` `…/stop/`, `edit` `…/edit/`,
  `delete` `…/delete/`, `export` `…/export.csv`.
- `active_nav = "sessions"`.
