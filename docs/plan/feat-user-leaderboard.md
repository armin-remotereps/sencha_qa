# feat: User leaderboard

Status: Done
Created: 2026-10-30
Request: The Insights page (machine health) is not useful to the user. They want a
leaderboard page that every logged-in user can open. It shows how each user is
doing: how many cases they got to Passed, Failed, Blocked, Needs re-test and so
on, how many tests are still remaining on their projects, and a light,
competitive layer (score, rank, medals, movement, streaks, badges). Filters
narrow it down. The period is a switch between exactly two views: **Today**
(from the start of the current day in the configured `TIME_ZONE`) and
**Last 7 days** (the default). Movement compares each view with the equal window
just before it.

## Context

- **Insights today.** The nav entry `◔ Insights` (`templates/partials/nav_links.html`)
  opens `dashboard:machine_health` (`dashboard/health_views.py`,
  `dashboard/health_forms.py`, `dashboard/services/machine_health.py`,
  `templates/dashboard/machine_health.html`, `active_nav="insights"`). The
  Failures widget on the dashboard also links to Machine health
  (`templates/dashboard/includes/_failed_widget.html:48`).
- **Cross-project read pages** live in `dashboard` and take their scope from
  `dashboard/services/member_projects.py`. Their filter forms receive the
  viewer's member projects as choices, so the choices themselves authorize the
  filter (`MachineHealthFilterForm.filters()`). The leaderboard copies that
  pattern.
- **Case statuses.** `TestRunTestCaseStatus` (`projects/models.py:103`) has
  `created`, `in_progress`, `success`, `failed`, `cancelled`, `blocked`,
  `need_help`, `awaiting_person` and `needs_retest`. `FINISHED_CASE_STATUSES` is
  success, failed, blocked, cancelled and needs_retest. `finished_at` is set when
  a verdict is written. A Needs re-test mark keeps the earlier `finished_at`, or
  sets it to the marking time when the case had no verdict
  (`projects/case_retest_marking.apply_retest_mark`). There is an index
  `run_case_status_finished_idx (status, -finished_at)`.
- **Run statuses.** `TestRunStatus` is waiting (Draft), started, done, cancelled
  and paused (Needs help). `TestRun.help_mode` is attended, night or hitl.
- **Time zone.** `auto_tester/settings.py:129` has `TIME_ZONE = "UTC"`,
  hard-coded and not read from `.env`. `USE_TZ = True`, and
  `CELERY_TIMEZONE = TIME_ZONE`. "Today" therefore starts at 00:00 UTC today.
- **Users.** `accounts.CustomUser` has `email`, `first_name`, `last_name` and
  `is_active`. Elsewhere the code shows a user as `get_full_name() or email`
  (`projects/case_result_text.py:42` and others).
- **Membership.** `ProjectMembership(project, user, role)`.
  `WRITABLE_ROLES = {member}`. Read-only members cannot start or act on runs.

### Who did something to a case: inventory

| Where | Actor stored | Covers | Gap |
|---|---|---|---|
| `start_test_run(test_run, *, user, help_mode)` (`projects/services.py:2542`) | **Not stored on `TestRun`**. `user` only reaches `record_case_activity` | Every case of the run | Start actor is lost unless the user had a test session open |
| `TestRunCaseResume.resumed_by` (+ `kind` resume / rerun / redesign) | Yes, one row per resume, re-run or redesign re-run | Cases restarted in place | No row for the first Start |
| `CaseHelpRequest.resolved_by` / `resolved_at` / `resolution` | Yes (null for `auto_fixed`) | Continue, Mark failed, Mark blocked, Marked for re-test, Cancelled | Only cases that needed help |
| `CasePersonStep.reported_by` / `reported_at`, `closed_by` / `closed_via` | Yes, but null for an anonymous Test Runner answer | Human in the loop steps | Only HITL cases. A redo reuses the row (`round`), so `reported_at` holds the latest report only |
| `CaseResultChange.decided_by` (status `applied`) | Yes | Result changes made in result chat | A review action, not test execution |
| `TestRunTestCase.retest_marked_by` | Yes, while the case is Needs re-test | The re-test mark | A review action. Cleared by Re-run, Reset and Undo |
| `TestRunTestCase.checked_by` | Yes | The "Checked" mark for TestRail | A review action |
| `test_sessions.TestSessionCase` (`session.user`) | Yes, but **only while the user has a session open** | Every hook above | Empty when no session is open. Measures sessions, not results |
| `TestRunTestCase.failed_by` | No user, only agent / help / review | — | — |

**Conclusion.** Nothing stored today says reliably "this verdict belongs to user X".
The gap that breaks every derived rule is the Start actor: on the most common
path (a person starts a run and the AI or the person finishes it), the starter
is recorded only inside an open test session.

## Goals

- A new Leaderboard page that every logged-in user can open. It ranks every
  eligible user for Today or Last 7 days, with optional project and run-mode
  filters.
- Per user it shows:
  - Passed, Failed, Blocked and Needs re-test counts, and Tests done;
  - Steps performed and Help resolved;
  - Need help now (cases stuck on them right now);
  - Remaining (cases without a verdict in open runs of their projects);
  - score, rank, medal, movement, streak and badges.
- Each case result is credited to exactly one user, using a simple rule and
  one new nullable column.
- A constant number of queries, whatever the number of users or cases.

## Non-goals

- A ledger of every verdict ever written. Re-run, Reset and result changes
  replace the current verdict (see Alternatives).
- Live updates over WebSocket. The page is static; reload to refresh.
- Showing "credited to" on the run page or case page, or editing the credit by
  hand.
- Configurable points, seasons, history beyond 7 days, achievements stored in
  the database, notifications about rank changes.
- Changing Machine health. It stays as it is and remains reachable.
- A per-user time zone.

## Design

### Attribution rule (primary decision)

Add **`TestRunTestCase.credited_user`**. The rule is: *a case's result belongs to
the person who last drove it.* "Drove" means made it execute, performed its
steps, or got it out of Need help. The field is overwritten at each of these
actions:

| Action (service, at its existing `record_case_activity` call) | Sets credit to |
|---|---|
| Start (`projects/services.start_test_run`), all cases of the run | `user` |
| Resume (`projects/run_resume_services._resume_pivots`), resumed cases | `user` |
| Re-run selected (`projects/run_rerun_services.rerun_test_run_cases`) | `user` |
| Redesign re-run (`projects/case_review_redesign_services.rerun_with_redesigned_plan`) | `user` |
| Help resolution (`projects/case_help_services._resolve`): Continue, Mark failed, Mark blocked | `user` (the resolver) |
| Person step report, overrule, mark failed or mark blocked (`projects/person_step_services._after_action` with activity `PERSON_STEP`) | `actor.user`, **only when not None** |

These actions do **not** move the credit:

- Mark for re-test, the help and person-step re-test closes, and Undo. The
  marker is already in `retest_marked_by`.
- Applying a result change. This is a review: it fixes a result, it does not
  produce one.
- Checked and TestRail sends.
- Night auto-fix (`resolved_by` is null).
- Anonymous Test Runner answers.
- Reset. The field is left in place. A Reset case has no `finished_at`, so it
  counts nowhere, and the next Start overwrites it.

**Counting.** A case counts for `credited_user` in a window when
`finished_at ∈ window` and `status ∈ {success, failed, blocked, needs_retest}`.
Cancelled cases never count. The current verdict is what counts.

**What this rule gives up**

- A verdict that is replaced before you look counts only once, as its latest
  value. This applies to a re-run, resume or redesign re-run (the new verdict,
  at its new `finished_at`, is counted) and to Reset (nothing is counted until
  the case runs again). Upside: re-running the same case cannot farm points.
- When a different person applies a result change, the credit stays with the
  person who drove the case. The new status counts for them.
- When A starts a run and B rescues a case through help or a person step, B gets
  that case. "Last actor wins" is intentional: B finished it.
- **History from before the migration:** see Data model, backfill. Cases that
  no signal covers stay unattributed. The page shows them as a footnote count
  ("N results in this period are not credited to anyone").

### Data model

- `projects.TestRunTestCase.credited_user`:
  `ForeignKey(settings.AUTH_USER_MODEL, on_delete=SET_NULL, null=True, blank=True, related_name="+", db_index=False)`.
  - `db_index=False` because no query filters on the user alone. The window
    queries go through `finished_at` / `status`, and `run_case_status_finished_idx`
    serves them. It also avoids an index build on a populated table.
  - Adding a nullable column with no default is a metadata-only change in
    Postgres.
- Migration `projects/0046_run_case_credited_user` (schema only, hand-checked
  with `sqlmigrate`: one `ADD COLUMN`, FK constraint).
- Migration `projects/0047_backfill_case_credit` (RunPython, reverse no-op,
  `atomic=False`, batches of 1,000):
  - **Scope:** cases with `finished_at >= now - 14 days` (the current and
    previous 7-day windows at deploy time), plus every case whose status is
    `need_help` or `awaiting_person`.
  - **Rule:** the user of the most recent of four signals:
    - `TestRunCaseResume.resumed_by` (`created_at`);
    - `CaseHelpRequest.resolved_by`, excluding `marked_retest` (`resolved_at`);
    - `CasePersonStep.reported_by` (`reported_at`);
    - `TestSessionCase.session.user`, where the activity is not `retest` or
      `result_change` (`last_activity_at`).
  - **Queries:** four `values_list` queries over the scoped case ids, merged in
    Python, then `bulk_update(["credited_user"])`.
  - **Dependency:** `test_sessions.0002_needs_retest`. This adds no cycle,
    because `test_sessions` migrations do not depend on `projects/0046+`.
  - Runs started before the deploy, outside any session and never touched
    again, stay unattributed.
- `projects/admin.py`: add `credited_user` to the case admin's read-only fields.
  One line, optional.

### Services

New code goes in `dashboard`, the app that already owns cross-project reporting.
`dashboard` imports `projects.models`. `projects` never imports `dashboard`.
There is no new app: the leaderboard has no models of its own, and the credit
field belongs to the run case.

1. **`projects/case_credit.py`** (new, leaf: imports only `projects.models` and
   `accounts.models`).
   - `credit_cases(*, user: CustomUser | None, pivot_ids: Sequence[int]) -> None`
     - Does nothing when `user is None` or there are no ids.
     - Otherwise runs one `TestRunTestCase.objects.filter(pk__in=ids).update(credited_user=user)`.
       `update()` does not bump `updated_at`. That is deliberate: credit is not a
       user-visible change.
   - Runs inside the caller's transaction, and errors propagate. Unlike session
     recording, credit is part of the action.
   - Called right next to the matching `record_case_activity` call at each hook
     in the table above. That is one extra query per action.
   - In `_resume_pivots`, `rerun_test_run_cases` and the redesign re-run, the
     cases are saved after the hook. Those saves use explicit `update_fields`
     that do not include `credited_user`, so they do not overwrite it. The
     implementer confirms this at each site. Where the service saves the locked
     instance with `update_fields`, setting `pivot.credited_user = user` in memory
     and adding the field to `update_fields` is equally fine and saves the query.

2. **`dashboard/leaderboard_types.py`** (new, shared, written first). Frozen
   dataclasses and enums:
   - `LeaderboardPeriod(StrEnum)`: `TODAY = "today"`, `WEEK = "7d"`.
     `DEFAULT_PERIOD = WEEK`.
   - `LeaderboardWindow`: `period`, `start`, `end` (now), `previous_start`,
     `previous_end`, `days: int` (1 or 7), `time_zone_name: str`.
   - `LeaderboardFilters`: `period: LeaderboardPeriod`, `project_id: int | None`,
     `help_mode: str | None`.
   - `UserTally`: raw numbers for one user and one window. `passed`, `failed`,
     `blocked`, `needs_retest`, `steps_performed`, `help_resolved`, all `int`.
     Property `tests_done`.
   - `Badge`: `key: str`, `label: str`, `title: str` (the tooltip text).
   - `LeaderboardRow`:
     - who: `user_id`, `name`, `is_viewer`;
     - this window's raw numbers: `current: UserTally`;
     - position and scoring: `score`, `previous_score`, `rank`,
       `previous_rank: int | None`, `movement: int | None`, `is_new: bool`,
       `medal: Literal["gold", "silver", "bronze", ""]`;
     - not windowed: `need_help_now`, `remaining`;
     - activity: `active_days`, `streak`, `badges: tuple[Badge, ...]`.
   - `Leaderboard`:
     - `window: LeaderboardWindow`, `filters: LeaderboardFilters`;
     - rows: `rows: list[LeaderboardRow]`, `podium: list[LeaderboardRow]` (at
       most 3, `score > 0`), `viewer_row: LeaderboardRow | None`;
     - `unattributed_count: int`;
     - `points: tuple[tuple[str, int], ...]` (the point rules, so the page can
       print "How points work" from the single source).

3. **`dashboard/leaderboard_rules.py`** (new, pure domain rules, no ORM):
   - `POINTS`: `passed 2`, `failed 2`, `blocked 1`, `needs_retest 0`,
     `step_performed 1`, `help_resolved 1`.
   - `resolve_window(period: LeaderboardPeriod, now: datetime) -> LeaderboardWindow`
     - Works on `timezone.localtime(now)`, which uses `settings.TIME_ZONE`.
     - `start` is local midnight of `today - (days - 1)`, and `end` is `now`.
     - `previous_start = start - days` and `previous_end = now - days` (calendar
       days, built with `datetime.combine(..., tzinfo=current tz)`, so it is DST
       safe if the zone ever changes).
     - Today compares against yesterday 00:00 up to yesterday at this time.
       Last 7 days compares against the 7 days before, up to the same time of
       day.
   - `score(tally: UserTally) -> int`.
   - `rank_rows(...)`
     - Order: score descending, then `tests_done` descending, then name.
     - Standard competition ranking: equal scores share a rank (1, 1, 3).
     - Users with score 0 rank after everyone else, with no medal and no
       movement arrow.
   - `movement(rank, previous_rank, previous_score) -> tuple[int | None, bool]`
     returns the places gained (positive is up) and `is_new`. A user is new when
     the previous score is 0 and the current score is above 0. The previous ranks
     come from ranking the same user set by previous scores.
   - `streak(days_with_activity: set[date], today: date, window_days: int) -> int`
     - Counts consecutive local days with at least one counted verdict or
       reported person step, ending today.
     - If today has nothing yet, it ends yesterday, so the streak does not
       break until the day is over.
     - Capped at `window_days`. Shown only in the 7-day view.
   - `badges(rows, window) -> per-row badges`. Ties all get the badge, and a
     value of 0 never earns one.
     - *Bug hunter*: most Failed.
     - *Hands-on*: most Steps performed.
     - *Helper*: most Help resolved.
     - *Perfect week*: active on all 7 days (7-day view only).

4. **`dashboard/services/leaderboard.py`** (new, the use case):
   - `get_leaderboard(*, viewer: CustomUser, filters: LeaderboardFilters, now: datetime | None = None) -> Leaderboard`.
   - It runs the query plan below, then assembles the result with
     `leaderboard_rules`.
   - Eligible users:
     - active users with at least one writable membership in a non-archived
       project (in the filtered project, if a project filter is set);
     - plus active users credited with any case in the window and filters
       (former members, read-only members who did work earlier).
     - Inactive users are never listed. Their results drop out.
   - Name: `get_full_name()` or `email` (see Q4).

#### Query plan (9 queries, constant)

`F` is the case filter shared by queries 3–7 and 9: the project filter
(`test_run__project_id`), the run-mode filter (`test_run__help_mode`), and
`test_run__project__archived=False` only for the snapshot metrics (7 and 9).
Windowed history from archived projects still counts.

1. **Viewer's project choices.** `member_project_choices(viewer)` (existing,
   1 query). Used by the form.
2. **Eligible users.**
   `CustomUser.objects.filter(is_active=True).filter(Q(writable membership …) | Q(pk__in=Subquery(credited ids in [previous_start, end) under F)))`,
   `.distinct()`, `.only("id", "email", "first_name", "last_name")`.
3. **Verdicts.** `TestRunTestCase` with `finished_at ∈ [previous_start, end)`,
   `status ∈ COUNTED`, under F, `.values("credited_user_id")`, annotated with
   conditional `Count`s:
   - for the current window, `passed`, `failed`, `blocked`, `needs_retest`;
   - for the previous window, `prev_passed`, `prev_failed`, `prev_blocked`.

   The `credited_user_id IS NULL` group gives `unattributed_count` (current
   window). The planner can use `run_case_status_finished_idx`.
4. **Person steps.** `CasePersonStep` with `reported_by` not null and
   `reported_at ∈ [previous_start, end)`, under F (through
   `test_run_test_case__test_run`), `.values("reported_by_id")` with current and
   previous `Count`s.
5. **Help resolved.** `CaseHelpRequest` with `resolved_by` not null, `resolution`
   in continued, marked_failed or skipped, `resolved_at ∈ [previous_start, end)`,
   under F, `.values("resolved_by_id")` with current and previous `Count`s.
6. **Active days.** One `UNION` of two `.values_list(user_id, TruncDate(ts, tzinfo=current tz)).distinct()`
   querysets for the current window: counted verdicts by `credited_user_id`, and
   person steps by `reported_by_id`. At most users × 7 rows. Skipped (0 queries)
   in the Today view.
7. **Need help now.** `TestRunTestCase` with
   `status ∈ {need_help, awaiting_person}`, `credited_user` not null, under F,
   `.values("credited_user_id").annotate(n=Count("id"))`. This is a snapshot,
   not windowed.
8. **Writable memberships.** `ProjectMembership` with
   `role ∈ WRITABLE_ROLES`, `project__archived=False` (and the project filter),
   `values_list("user_id", "project_id")`.
9. **Remaining per project.** `TestRunTestCase` with
   `test_run__status ∈ {waiting, started, paused}` and
   `status ∉ FINISHED_CASE_STATUSES`, under F,
   `.values("test_run__project_id").annotate(n=Count("id"))`.
   - Per user, Remaining is the sum over their writable memberships (8 × 9,
     done in Python).

No per-user query and no N+1. Rows, aggregation and ranking are
O(users + memberships).

#### Metric definitions

Window metrics, in [`start`, `end`); the same metrics in the previous window
feed movement:

| Metric | Definition |
|---|---|
| Passed / Failed / Blocked / Needs re-test | Cases credited to the user, current status `success` / `failed` / `blocked` / `needs_retest`, `finished_at` in the window |
| Tests done | The sum of the four statuses above |
| Steps performed | `CasePersonStep` rows with `reported_by = user` and `reported_at` in the window. One per step: a redo reuses the row, so only its latest report counts |
| Help resolved | `CaseHelpRequest` with `resolved_by = user`, resolution Continue, Mark failed or Mark blocked, `resolved_at` in the window |
| Score | 2·Passed + 2·Failed + 1·Blocked + 0·Needs re-test + 1·Steps performed + 1·Help resolved |
| Active days / streak | Distinct local days with a counted verdict or a reported step (7-day view only) |

Snapshot metrics, as of now, not windowed:

| Metric | Definition |
|---|---|
| Need help now | Cases credited to the user whose status is `need_help` or `awaiting_person` |
| Remaining | Cases with no verdict (`created`, `in_progress`, `need_help`, `awaiting_person`) in runs that are Draft, Running or Needs help, in non-archived projects where the user is a writable member. A shared project counts for each of its members. Cancelled and Done runs do not count. Needs re-test cases are a settled status and not counted here (see Q6) |

All metrics follow the project and run-mode filters. The run-mode filter uses
the run's current `help_mode` (a mid-run mode switch re-labels the whole run).

### Pages and forms

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `/leaderboard/` → `dashboard:leaderboard` | GET (`@login_required`, `@require_GET`) | Any logged-in user | `LeaderboardFilterForm` (GET: `period`, `project`, `mode`) | `dashboard/leaderboard.html`: `form`, `board: Leaderboard`, `active_nav: "insights"` | Invalid values fall back to the defaults (as `MachineHealthFilterForm.filters()` does). A project the viewer is not a member of is not a valid choice, so it is ignored and the page shows all projects |

- **`dashboard/leaderboard_forms.py`**: `LeaderboardFilterForm(project_choices, data=None)`.
  - `period`: `ChoiceField`, choices from `LeaderboardPeriod`, default `7d`,
    `HiddenInput`. The tabs are links, and the hidden field keeps the period
    when Apply is pressed.
  - `project`: `TypedChoiceField(coerce=int)`, with "All projects" plus the
    viewer's member projects.
  - `mode`: `ChoiceField`, with "All run modes" plus `RunHelpMode.choices`.
  - `filters() -> LeaderboardFilters`.
- **`dashboard/leaderboard_views.py`**: `leaderboard(request: AuthenticatedRequest) -> HttpResponse`.
  It builds the form from `member_project_choices`, calls `get_leaderboard`
  and renders. It holds no logic.
- **`dashboard/urls.py`**: `path("leaderboard/", leaderboard_views.leaderboard, name="leaderboard")`.
- **`templates/dashboard/leaderboard.html`**, extending `layouts/app.html`:
  1. **Header.** "Leaderboard", the window as a subtitle
     ("Today since 00:00 UTC" / "Mar 24 – Mar 30"), and a secondary link to
     Machine health.
  2. **Period tabs.** `<nav class="tabs">` with two links built with Django's
     `{% querystring period="today" %}` / `{% querystring period="7d" %}`
     (Django 5.1+; the request context processor is enabled), using
     `aria-current`.
  3. **Toolbar form.** Project and run-mode selects, the hidden period, and
     Apply.
  4. **Podium** (`includes/_leaderboard_podium.html`). Up to 3 cards, each with
     a medal, name, score, a movement chip and badges. The viewer's card is
     marked "You".
  5. **Table** (`includes/_leaderboard_table.html`). A `.data-table` with
     `data-label` cells for mobile:
     - columns: rank (with medal), name, score, movement (▲n / ▼n / "New" / "–"),
       Passed, Failed, Blocked, Needs re-test, Tests done, Steps, Help,
       Streak (7-day view only), Need help now, Remaining, badges;
     - the viewer's row is highlighted;
     - users with score 0 are listed below the others, dimmed.
  6. **"Your position" strip.** Shown when the viewer is ranked below 10.
  7. **Footnotes.** The unattributed count, "How points work" (from
     `board.points`), and "Remaining counts open runs in your teammates'
     projects; names are not shown".
  8. **Empty state.** Shown when no one has a score: "No results yet today",
     with a link to Last 7 days.

  Every value is auto-escaped. There is no `|safe` and no Alpine.js beyond what
  the layout already loads. A `<details>` element is enough for "How points
  work".
- **CSS**, added to `static/css/punk-hazard.css` with design tokens only:
  `.leaderboard-podium`, `.podium-card`, `.medal`, `.medal-gold`, `.medal-silver`,
  `.medal-bronze`, `.rank-move`, `.rank-move-up`, `.rank-move-down`, `.rank-new`,
  `.lb-badge` (or reuse `.chip`), `.data-table tr.is-viewer`,
  `.data-table tr.is-idle`.
- **Nav.** `templates/partials/nav_links.html`: the Insights link becomes
  `★ Leaderboard`, pointing to `dashboard:leaderboard`, with
  `active_nav == "insights"` kept so Machine health still highlights it. Update
  the `active_nav` contract comments in `nav_links.html`, `sidebar.html`,
  `topbar.html` and `layouts/app.html` only if a value changes (it does not).
  Machine health stays at `/insights/machines/`. It is linked from the
  leaderboard header and the dashboard Failures widget.

### Background work and WebSockets

None. No Celery task, consumer, socket event or controller action. No
controller protocol change, so old controllers are unaffected.

### Settings / infrastructure

- No new env var in the recommended option. "Today" uses
  `timezone.localtime()`, so it follows `settings.TIME_ZONE`, which is UTC
  today. The subtitle names the zone so the boundary is not a surprise. Q3
  covers making it configurable.
- Deploy: `migrate`, then restart Daphne/web and every Celery worker. Workers
  load `projects.services`, which gains the `credit_cases` call. An old web
  process would start runs without setting credit.

### Data flow

1. A person presses Start, Resume or Re-run, resolves help, or reports a person
   step. The service locks, writes, calls `record_case_activity` (sessions) and
   `credit_cases` (credit). Both are in the same transaction.
2. A worker later writes the verdict and `finished_at`. Credit is untouched.
3. A user opens `/leaderboard/?period=7d&project=&mode=`. The view validates the
   GET through `LeaderboardFilterForm` and calls
   `get_leaderboard(viewer, filters)`.
4. The service resolves the window with `resolve_window`, runs queries 2–9,
   merges the per-user tallies for both windows, then scores, ranks, computes
   movement, streaks and badges, and returns `Leaderboard`.
5. The template renders the tabs, filters, podium and table.

## Alternatives considered

- **Verdict ledger (`CaseVerdictCredit` row per verdict written).**
  - Pros: exact history, survives re-runs and Reset, and could count every
    attempt.
  - Cons: every verdict write site needs an actor, including the worker's
    `_finalize_pivot`, which has none, so a credit field is needed anyway. It
    also means many more write sites, and re-running a case would farm points.
  - Rejected for now. It can be added later on top of `credited_user`.
- **Derive credit at read time from existing tables** (resumes, help requests,
  person steps, sessions).
  - No migration, but the Start actor is missing, which is the most common
    path. It would also need four correlated subqueries per case.
  - Rejected.
- **`TestRun.started_by` only.** Loses resumes, re-runs, help rescues and HITL
  steps done by other people. HITL is the default run mode, so most real work
  would go to whoever pressed Start. Rejected.
- **Count `TestSessionCase` rows.** Only covers work done while a session is
  open, so the leaderboard would measure session discipline instead of results.
  Rejected. Sessions stay the timesheet feature.
- **A new `leaderboard` Django app.** It would have no models. `dashboard`
  already owns cross-project reporting (failures, machine health), and the
  credit belongs to the run case. Rejected to avoid an empty app.
- **Live socket updates.** Not needed for a page people glance at. Rejected
  (simplicity).

## Risks and mitigations

- **Migration on a populated table.**
  - 0046 is a nullable `ADD COLUMN` with no index, which is metadata-only.
  - 0047 touches only about 14 days of cases plus the open ones, in batches,
    with a no-op reverse.
  - Review `sqlmigrate projects 0046` before deploying.
- **Wrong or missing attribution.** History before the deploy is partial. The
  page states the unattributed count rather than guessing. Anonymous runner
  answers keep the previous credit.
- **Credit write failing an action.** One indexed-PK `UPDATE` inside a
  transaction that is already open. If it fails, the action fails, which is
  correct because the same transaction already wrote the case.
- **Hook drift.** A future "drive" action could forget `credit_cases`. Mitigate
  with a docstring in `case_credit.py` listing the hook sites, and with a note
  in the change doc.
- **Privacy.** Every user sees every eligible user's name and numbers. With the
  recommended options:
  - no project names leak (the project filter lists only the viewer's
    projects);
  - Remaining is a bare number.

  Q2 and Q4 cover what is shown.
- **Fairness and gaming.** One Start of a 300-case Attended run credits 300
  cases to one click, while a HITL tester's 10 cases take all day. Mitigations:
  - the run-mode filter;
  - Steps performed and Help resolved also earn points;
  - re-runs replace verdicts, so they do not add points.

  Q1 asks whether to weight by mode.
- **XSS.** User names are auto-escaped, and there is no `|safe`.
- **Performance.** 9 queries, all aggregates. Query 2's `OR Subquery` and
  query 3 scan cases finished in the last 14 days (or 2 days) through
  `run_case_status_finished_idx`. Check with `EXPLAIN` on the dev database.
- **Time zone.** UTC midnight may not match the team's day. This is shown on
  the page (Q3).

## Implementation steps

Step 1 is shared and comes first. Steps 2–5 can run in parallel; each owns its
files. Step 6 is integration.

1. **Shared foundation (orchestrator).**
   - Files: `projects/models.py` (`credited_user`),
     `projects/migrations/0046_run_case_credited_user.py`,
     `projects/migrations/0047_backfill_case_credit.py`,
     `projects/case_credit.py`, `dashboard/leaderboard_types.py`.
   - Check: `makemigrations --check`, `sqlmigrate`, mypy.
2. **Credit hooks (sub-agent A).** Call `credit_cases` at the six sites listed
   in the Attribution table, next to the existing `record_case_activity`.
   - Files: `projects/services.py`, `projects/run_resume_services.py`,
     `projects/run_rerun_services.py`,
     `projects/case_review_redesign_services.py`,
     `projects/case_help_services.py` (`_resolve`, skipping `MARKED_RETEST`),
     `projects/person_step_services.py` (`_after_action`, only for the
     `PERSON_STEP` activity), and `projects/admin.py` (read-only field).
   - Must not touch the result change, re-test or Undo paths.
3. **Rules (sub-agent B).** `dashboard/leaderboard_rules.py`: the pure functions
   with the exact signatures in Services §3. Consumes `leaderboard_types`.
4. **Service (sub-agent C).** `dashboard/services/leaderboard.py`:
   `get_leaderboard` and the 9-query plan. Consumes `leaderboard_types` and
   `leaderboard_rules` (signatures fixed in this plan) and
   `member_projects.member_project_choices` (not called here; the view passes
   choices to the form only).
5. **Page (sub-agent D).**
   - Files: `dashboard/leaderboard_forms.py`, `dashboard/leaderboard_views.py`,
     `dashboard/urls.py`, `templates/dashboard/leaderboard.html`,
     `templates/dashboard/includes/_leaderboard_podium.html`,
     `templates/dashboard/includes/_leaderboard_table.html`, the CSS additions
     in `static/css/punk-hazard.css`, and `templates/partials/nav_links.html`.
   - Context contract: `form`, `board`, `active_nav="insights"`.
6. **Integration (orchestrator).** Read the combined diff, check that the
   imports match, run `pre-commit run --all-files`, `manage.py check` and
   `makemigrations --check`.

## Verification

No automated tests (project rule). Manual checks, against a throwaway or dev
database:

- **Migrations.** `python manage.py migrate` forwards and backwards on a copy
  of the dev database. Confirm `sqlmigrate projects 0046` is a single
  `ADD COLUMN` plus the FK. The backfill sets credit on cases that have a
  resume, help, step or session signal.
- **Credit hooks** (smoke script in `manage.py shell` inside `transaction.atomic()`
  with a forced rollback):
  - each of Start, Resume, Re-run, Redesign re-run, help Continue / Mark failed
    / Mark blocked, and a person step report sets `credited_user`;
  - Mark for re-test, Undo, applying a result change and an anonymous runner
    answer do not.
- **Metrics.** With fixture rows across both windows, check Passed / Failed /
  Blocked / Needs re-test, Steps, Help, Need help now and Remaining against
  hand counts. Check the Today boundary at 00:00 (UTC) and the previous-window
  ends (yesterday at the same time; 7 days earlier at the same time).
- **Query count.** `CaptureQueriesContext` around `get_leaderboard`: 9 (8 for
  Today) with 2 and with 50 users.
- **Ranking.** Ties share a rank; medals go only to scores above 0; movement
  arrows and "New" are right; badges are shared on ties; Perfect week appears
  only in the 7-day view.
- **Visibility and filters:**
  - a read-only member and a user with no projects can open the page;
  - the project filter lists only the viewer's projects;
  - `?project=<foreign id>` falls back to all projects;
  - `?period=bogus` falls back to 7d;
  - the run-mode filter changes the counts.
- **Browser** on a unique port (e.g. `daphne -p 8014`):
  - nav shows Leaderboard, and Machine health is still reachable and
    highlights it;
  - the tabs keep the filters;
  - mobile card layout of the table;
  - a name with `<script>` renders escaped.
- `pre-commit run --all-files` (isort, black, strict mypy).
- Run the `reviewer` and `verifier` agents in parallel on the diff.

## Open questions

- **Q1 — Point rules.**
  - Should Failed earn more than Passed (finding bugs is the job)?
  - Should HITL cases be weighted higher than AI-run cases, given that one
    Start can credit hundreds of cases?

  *Recommended / assumed:* Passed 2, Failed 2, Blocked 1, Needs re-test 0,
  Step performed 1, Help resolved 1. No weighting by mode; the run-mode filter
  gives like-for-like comparisons.
- **Q2 — Visibility of projects.** "All projects" totals include projects the
  viewer is not a member of, as bare numbers (Tests done, Remaining). The
  project filter lists only the viewer's own projects, and no foreign project
  name is shown anywhere.
  - Alternative: limit every number to the viewer's projects. That is more
    private, but each viewer then sees a different board.

  *Recommended / assumed:* global numbers, project filter limited to the
  viewer's projects.
- **Q3 — "Today" boundary.** `TIME_ZONE` is hard-coded to `"UTC"`, so Today
  starts at 00:00 UTC. Making the global `TIME_ZONE` env-driven would also move
  Celery beat schedules and every displayed time.

  *Recommended / assumed:* keep UTC and label it on the page. If the team needs
  a local day, add a dedicated `LEADERBOARD_TIME_ZONE` setting
  (python-decouple, default `TIME_ZONE`, added to `example.env`) used only by
  `resolve_window`.
- **Q4 — Who is listed and how they are named.** Rows are active users with a
  writable membership in a non-archived project, plus anyone credited in the
  window. Read-only-only users (possibly external viewers) can open the page
  but are not listed. Names are `get_full_name()`, falling back to the email.
  - Alternatives: list every active user, including read-only-only users; or
    never show emails (use "first name + initial", or the email's local part).

  *Recommended / assumed:* as described, with the email fallback.
- **Q5 — Navigation.**

  *Recommended / assumed:* the Insights nav entry becomes "Leaderboard"
  (`/leaderboard/`). Machine health stays at its URL, linked from the
  leaderboard header and the dashboard Failures widget, and keeps highlighting
  the same nav item.
- **Q6 — Remaining scope.** Should Remaining count Draft runs, and should Needs
  re-test cases count as remaining?

  *Recommended / assumed:* count cases without a verdict in Draft, Running and
  Needs help runs. Needs re-test is not counted here; it is shown in its own
  column within the window.
- **Q7 — Credit on applied result change.** Should applying a result change
  move the credit to the reviewer?

  *Recommended / assumed:* no. The case stays with whoever drove it, and the
  reviewer is not rewarded for changing someone else's result.
- **Q8 — Backfill.**

  *Recommended / assumed:* a best-effort backfill of the last 14 days, plus open
  cases, from resume, help, person-step and session signals, so the first week
  is not empty. Runs started outside a session stay unattributed and appear in
  the footnote count.

## Changelog

- 2026-10-30: created (period filter is exactly Today / Last 7 days per user
  follow-up; previous window = the equal span immediately before).
- 2026-10-30: user accepted the recommended answers for Q1 (points), Q2 (global
  numbers, project filter limited to own projects), Q3 (UTC, labelled) and Q5
  (Insights nav becomes Leaderboard); Q4, Q6, Q7 and Q8 take the assumed answers.
- 2026-10-30: review round 1. Fixed: indexes on `CaseHelpRequest.resolved_at` and
  `CasePersonStep.reported_at` (0048, concurrently); verdicts read before users
  (one case scan); `case_credit.CREDITED_HELP_RESOLUTIONS` is the single source
  for the credit and "Help resolved"; backfill uses that set (a cancel no longer
  credits) and streams rows; shared `_leaderboard_move.html` / `_leaderboard_medal.html`;
  CSS classes prefixed `lb-`; results of unlisted (deactivated) users join the
  footnote count; the table stays under the empty state so Remaining shows.
  Rejected: folding the credit into the resume / redesign saves (one extra
  one-row UPDATE keeps every hook identical); hiding emails (Q4 default with the
  email fallback was accepted).
