# feat: Superuser access to every project and an "All projects" page

Status: Done
Created: 2026-11-01
Request: "Admin should have access to all projects with filter and search ability
through another page, and can controll all". An active superuser ("admin", the
existing `is_superuser` concept) can open and act on every project as if they were a
writable member, whether or not they belong to it, and gets a new superuser-only page
that lists every project in the installation with search, filters and pagination, as
the way in to those projects. Their normal Projects page keeps showing only the
projects they belong to.

## Context

- Today access is membership-only. `projects/project_access_services.get_project_access`
  (one query on `ProjectMembership`) returns `ProjectAccess(project, role)`
  (`projects/project_access.py`). `can_write` means `role in WRITABLE_ROLES`.
  Everything else builds on it: `projects/decorators.py`
  (`project_membership_required`, `project_read_access_required`,
  `get_writable_project`), the `project_access` context processor
  (`project_can_write`, `project_role`) and `projects/layouts/project.html`, which shows
  the "Read-only access" badge.
- This reverses an earlier decision. `docs/plan/feat-user-management.md` Q3 chose "no
  implicit superuser access", and `docs/changed/2026-10-29-user-management.md` says:
  "Superusers only get superuser powers in projects they are members of". This plan
  replaces that rule for project access only. Notifications, attention items and
  member invariants stay membership-based (see Non-goals).
- Superuser powers that exist today: the Users page (`accounts/user_views.py`,
  `accounts/decorators.superuser_required`, `accounts/user_management_services.py`)
  and run-mode choice (`projects/run_help_mode_policy.resolve_help_mode`,
  `run_help_mode_services.change_run_help_mode`,
  `project_run_mode_services.change_project_run_mode`, all of which check
  `user.is_superuser`). Templates gate run-mode controls on
  `user.is_superuser and project_can_write`.
- Project socket authorization: all project consumers (`UploadProgressConsumer`,
  `TestRunConsumer`, `TestRunCaseConsumer`, `AgentStatusConsumer` in
  `projects/consumers.py`, `KnowledgeBaseConsumer`, `MachineSetupConsumer`) call
  `AuthenticatedConsumer._get_project` → `projects.services.get_project_for_user`, which
  does its own `members=user` query and does not use `get_project_access`.
  `AttentionConsumer` is per user. `VisionWorkerConsumer` and the controller consumer
  authenticate with tokens or keys, not users.
- Cross-project pages are scoped by membership through
  `dashboard/services/member_projects.py`: the dashboard, `/failures/`, machine health
  and the leaderboard.
- The project list (`projects.views.project_list` → `projects.services.list_projects_for_user`,
  `templates/projects/includes/_project_table.html`) is membership-scoped and annotates
  `viewer_can_write`. The row actions `project_edit`, `project_archive`,
  `project_restore` and `project_duplicate` use `get_writable_project` and always
  redirect to `projects:list`.
- Two places check the actor's membership again inside a service, separately from
  the decorator. In `projects/project_member_services.py`, `_ensure_writer` checks
  invite and role changes and raises `MemberManagementNotAllowedError`. In the same
  module, `list_project_members` computes `viewer_can_write` and `can_leave` from the
  viewer's own membership row.
- Membership checks that run at task or runner time: in
  `machine_setup/services/open_paused_help_chat._writable_member`, which uses
  `get_project_access`, and `projects/runner_person_step_services.current_member` /
  `resolve_runner_member` (runner email to member).
- `test_sessions/services/case_outcome._owner_is_member` shows live run-case values
  only while the session owner is a member. `test_sessions/services/session_queries.list_session_cases`
  links a case only if the viewer is a member of its project.
- The cross-project app with a `services/` package is `dashboard`. It already hosts
  `/failures/`, `/leaderboard/` and `/insights/machines/` with GET filter forms
  (`dashboard/leaderboard_forms.py`, `health_forms.py`) and imports `projects.models`,
  `projects.services` and other `projects.*` modules.
- Versions: Django ~=5.2.11, channels ~=4.3.2, django-stubs ~=5.2.9
  (`requirements.txt`). Everything here uses standard ORM features: `Subquery`, `Exists`,
  `OuterRef`, `Paginator`, and `Count` inside a subquery.

## Goals

- An active superuser has writable access to every non-archived project, whether or
  not they are a member: every project page, every POST action, and every project
  WebSocket. Archived projects can be restored (`include_archived=True` paths). This is
  done once, in the central access resolution, and not view by view.
- A superuser who is a read-only member is also writable ("control all").
- On project pages, a superuser acting without a writable membership sees an
  "Admin access" badge, so they know they are not a member.
- A new superuser-only page, `/all-projects/`, lists every project. It has a text
  search (project name, member email, tag name), filters (status, Test Runner, active
  run, my membership), a sort and pagination. Each page uses a constant number of
  queries.
- Member management by a superuser works on the Settings Members card (invite, change
  role). Leave is offered only to actual members.

## Non-goals

- No notifications (ntfy), inbox, night digest, missed-failure records, attention
  count or "needs attention" broadcasts for projects where the superuser is not a
  member. Recipient queries stay `role__in=WRITABLE_ROLES` memberships.
- The superuser does not count as a member. The "at least one active writable member"
  invariant, member counts, member lists and the leaderboard stay membership-only.
- The dashboard, `/failures/` (including its bulk action), machine health, the
  leaderboard and the personal `/projects/` list stay scoped to membership. The new
  page is how a superuser reaches other projects.
- A Test Runner email answer is matched only against writable members
  (`current_member`, `resolve_runner_member`). A superuser who is not a member and
  answers at a runner is still recorded as "a person at the Test Runner".
- The "Copy to project" target picker (`list_other_projects_for_user`) keeps listing
  only the user's writable projects.
- There is no new audit table or actor label (see Q3). Actions are attributed to the
  superuser's user, as for any member.
- No change to the controller protocol, the vision worker, settings or `example.env`.
  There are no new Celery tasks and no migration.
- No "remove member" or "join project" action. A superuser can already invite themself
  by email on the Members card.

## Design

### Data model

No change and no migration.

### Access resolution (the single source)

- `projects/project_access.py`:
  - Add a pure predicate `has_superuser_project_control(user: CustomUser) -> bool`
    that returns `user.is_active and user.is_superuser`. It is the one definition of
    "an admin who controls every project". Python code must use it, not repeat the
    check. The test_sessions ORM expression below mirrors it and must say so in a
    comment.
  - Extend `ProjectAccess` with `role: str` (the membership role, `""` when the user is
    not a member) and `via_superuser: bool`. The properties are:
    - `is_member`: `role != ""`
    - `can_write`: `role in WRITABLE_ROLES or via_superuser`
    - `is_admin_access`: `via_superuser and role not in WRITABLE_ROLES`, which drives
      the badge.
- `projects/project_access_services.get_project_access(project_id, user, *, include_archived=False)`:
  keep the signature and the single query. Query `Project` by pk (with
  `archived=False` unless `include_archived`) and annotate it with
  `viewer_role=Subquery(ProjectMembership(project=OuterRef("pk"), user=user).values("role")[:1])`.
  Return `None` when the project is missing, or when `viewer_role` is null and
  `has_superuser_project_control(user)` is false. Otherwise return
  `ProjectAccess(project, role=viewer_role or "", via_superuser=has_superuser_project_control(user))`.
  The rules for non-members stay the same: still `None`, so the decorator still
  returns 404. Typing: read the annotation as the codebase already does, with a narrow,
  commented `type: ignore[attr-defined]` or `django_stubs_ext.WithAnnotations`. Do not
  widen the ignore.
- `projects/services.get_project_for_user(project_id, user)` (the consumers' path):
  reimplement it on `get_project_access` (`access.project if access else None`) so
  sockets and pages follow one rule. `projects.services` may import
  `project_access_services`; check that the import does not create a cycle.
- `projects/context_processors.project_access`: add `project_admin_access`
  (`access.is_admin_access`). `project_role` stays the membership role.

Nothing else is needed for the decorators. `project_membership_required`,
`project_read_access_required` and `get_writable_project` all go through
`get_project_access`.

### Every access check and what changes

| # | Location | Today | Change |
|---|----------|-------|--------|
| 1 | `projects/project_access_services.get_project_access` | membership only | superuser fallback (above) |
| 2 | `projects/project_access.ProjectAccess` | `role` only | `via_superuser`, `is_member`, `is_admin_access`, `can_write` includes superuser |
| 3 | `projects/decorators.py` (all three) | via #1 | no code change, inherits the new access |
| 4 | `projects/services.get_project_for_user` (every project consumer: upload, run, run case, agent status, knowledge base, machine setup) | own `members=user` query | delegate to #1 |
| 5 | `projects/context_processors.project_access` | can_write / role | add `project_admin_access` |
| 6 | `templates/layouts/project.html` (+ the five wizard templates that show "Read-only access") | read-only badge | "Admin access" badge when `project_admin_access` |
| 7 | `projects/project_member_services._ensure_writer` | writable membership row | also pass when `has_superuser_project_control(actor)` |
| 8 | `projects/project_member_services.list_project_members` | `viewer_can_write` and `can_leave` from the viewer's row | take `viewer_access: ProjectAccess`. `viewer_can_write = viewer_access.can_write`. `can_leave` additionally requires the viewer to be a member. The invariant check is unchanged (it counts other writable members only) |
| 9 | `projects/testrail_views` (Settings) | passes `viewer=request.user` | pass the request's access; the template is unchanged (Leave already gated on `can_leave`) |
| 10 | `projects/project_member_services.leave_project` | `NotProjectMemberError` for non-members | no change (view already redirects); button hidden by #8 |
| 11 | `projects/views.test_case_copy_to_project` target check | `get_project_access` | no change: a superuser may copy into any project by id; the picker stays member-only (Non-goals) |
| 12 | `projects/views.project_edit/archive/restore/duplicate` | `get_writable_project`, redirect to `projects:list` | access inherits #1. `project_archive` and `project_restore` honour a validated `next` POST field (existing `_next_or_default`) so the All projects page returns to itself |
| 13 | `machine_setup/services/open_paused_help_chat._writable_member` | `get_project_access(...).can_write` | no code change: a superuser pauser's message is now sent as them (consistent with "control all") |
| 14 | `machine_setup/views.py` knowledge picker, `projects/views.py` TestRail picker (`access.can_write`) | via request access | no change |
| 15 | Run-mode policy (`run_help_mode_policy`, `run_help_mode_services`, `project_run_mode_services`, the templates' `user.is_superuser and project_can_write`) | superuser plus member | no code change: superusers now reach every project, so they can set run modes everywhere |
| 16 | `projects/runner_person_step_services.current_member`, `resolve_runner_member` (runner email, discussion, help action, pause, manual) | writable members | no change (Non-goals, Q2) |
| 17 | `notifications/services/recipients.py`, `night_digest.py`, `record_missed_failure.py`, `inbox_broadcast.py`; `dashboard/services/attention.py`; `dashboard/services/member_projects.py`; `dashboard/services/leaderboard.py` | memberships | no change (Non-goals, Q2) |
| 18 | `projects/services.list_projects_for_user`, `get_all_tags_for_user`, `list_other_projects_for_user` | memberships | no change |
| 19 | `test_sessions/services/case_outcome._owner_is_member` | owner has a membership row | becomes `_owner_has_access`: membership row exists **or** `session__user__is_superuser=True & session__user__is_active=True`. Otherwise a superuser's own session rows on non-member projects read "Not available" until frozen. Comment that it mirrors `has_superuser_project_control` |
| 20 | `test_sessions/services/session_queries.list_session_cases` case links | viewer member of an unarchived project | if `has_superuser_project_control(user)`, link every unarchived project on the page (same single query without `members=user`) |
| 21 | `knowledge_base` views and consumer | decorator, #4 | no change (inherits); import rule unchanged |
| 22 | `AttentionConsumer`, controller and vision worker consumers | per user, or key/token | no change |
| 23 | `test_sessions.services.record_case_activity` | no membership check | no change: a superuser's actions are recorded in their own open session |

### Services (All projects page)

New module `dashboard/services/all_projects.py`. It is one use case, a superuser
listing every project, and it is placed in the cross-project app that already has a
`services/` package.

- `list_all_projects(*, actor: CustomUser, filters: AllProjectsFilters, page: int, per_page: int) -> Page[AllProjectsRow]`
  - Raises `SuperuserRequiredError` (reuse `accounts.exceptions`) unless
    `has_superuser_project_control(actor)`, as defence in depth behind the view
    decorator.
  - Base queryset `Project.objects.all()`. Filters:
    - `status`: `active` (default, `archived=False`), `archived`, or `all`.
    - `runner`: `any`, `connected` (`agent_connected=True`), or `disconnected`.
    - `run`: `any`, `active` (`Exists(TestRun status__in ACTIVE_RUN_STATUSES)`), or
      `idle` (not that).
    - `membership`: `any`, `mine` (`Exists` membership of the actor), or `not_mine`.
    - `q` (trimmed, max 100 characters): `name__icontains`, OR `Exists` of a
      membership whose `user__email__icontains`, OR `Exists` of a tag whose
      `name__icontains`. Use `Exists`, not joins, so there are no duplicate rows and no
      `distinct()`.
  - Sort: `newest` (default, `-created_at, -id`), `name` (`Lower("name"), id`), or
    `activity` (latest run `created_at` desc, nulls last, then `-id`).
  - Row annotations are correlated subqueries, never `Count` over joins (that fans
    out):
    - `case_count`, `member_count`, `writable_member_count`
    - `viewer_role` (the actor's membership role or null)
    - `latest_run_id` (as `list_projects_for_user` does)
    - `latest_run_at` (only when sorting by activity)
  - Use `.only()` for the columns the row needs: id, name, archived, created_at,
    agent_connected, agent_client_version, last_connected_at.
  - Use `prefetch_related("tags")` for the page and reuse
    `projects.services.get_latest_runs_by_project` for the latest-run badges (one
    query).
  - Map to the frozen dataclass `AllProjectsRow`: id, name, archived, created_at,
    tag_names, case_count, member_count, writable_member_count, viewer_role_label
    ("Member" / "Read-only" / "Not a member"), runner_connected, runner_version,
    last_connected_at, latest_run (status, label, created_at, outcome counts, url or
    None). Return a `Page` of rows, as `list_users` does (the existing narrow
    `type: ignore[arg-type]` pattern).
  - Query budget per request: paginator count (1), page rows (1), tags prefetch (1),
    latest runs (1), and `get_visual_engine_status` (1, for the connected badge state):
    **5 queries, constant in page size**.
- `AllProjectsFilters` is a frozen dataclass of the enum values above, and the
  `StrEnum`s live in `dashboard/all_projects_types.py`, following the
  `leaderboard_types.py` pattern.

### Pages and forms

| URL | Method | Permission | Form | Template / context | Errors |
|-----|--------|-----------|------|--------------------|--------|
| `/all-projects/` (`dashboard:all_projects`) | GET | `accounts.decorators.superuser_required` (login plus active superuser, else 403) | `AllProjectsFilterForm` (GET, every field optional, invalid fields fall back to defaults and are reported, as `FailedCaseFilterForm` does): `q`, `status`, `runner`, `run`, `membership`, `sort`; `page` / `per_page` via `auto_tester.pagination` | `templates/dashboard/all_projects.html`; context: `form`, `rows` (Page of `AllProjectsRow`), `page_obj`, `elided_page_range`, `per_page`, `allowed_per_page`, `query_params` (current filters, urlencoded), `connected_machine_state`, `total_count`, `active_nav="all_projects"` | 403 non-superuser; invalid filter shown inline |
| `/projects/<id>/archive/` (existing) | POST | `get_writable_project` (now superuser-aware) | adds an optional `next` | redirect to a safe `next`, else `projects:list` | 404 / 403 as today |
| `/projects/<id>/restore/` (existing) | POST | same, `include_archived=True` | adds an optional `next` | same | same |

Page layout (design-system classes only, no inline styles):

- `page-header`: title "All projects" and the subtitle "Every project in Punk Hazard.
  As a superuser you can open and change any of them."
- A filter `card` holding a GET `<form>`: search input, four selects and a sort
  select, with Apply and Reset. There is no Alpine except `rowMenu` for the row
  actions.
- A `data-table` with `data-label` cells: Project (a link to `projects:detail` when
  not archived, an Archived badge, tags in muted text), Your access, Test machine
  (`partials/badge_machine.html`, as `_project_table.html` does, plus the runner
  version), Latest run (badge plus date, linked), Test cases, Members ("N, M
  writable"), and Created.
- Row actions: Open. Archive goes through a confirm dialog (the existing `dialog`
  convention in `templates/partials/README.md`). Restore applies to archived rows.
  Both POST with `next` set to the current URL. Edit and Duplicate stay inside the
  project and on `/projects/`.
- `partials/pagination.html` with `query_params` so the filters survive paging.
- Navigation: in `templates/partials/nav_links.html`, add
  `{% if is_superuser %}` "▦ All projects" next to Users. Update the contract comments
  in `nav_links.html`, `sidebar.html` and `topbar.html` (`active_nav` gains
  `"all_projects"`).
- Project pages: `templates/layouts/project.html` shows
  `<span class="badge badge-running" title="You are not a writable member of this project; you have access as a superuser.">Admin access</span>`
  when `project_admin_access`. Use whichever existing badge class fits; add none unless
  needed. The five wizard templates show the same badge where they show "Read-only
  access".

### Background work and WebSockets

There are no new tasks, queues or events. Project sockets authorize superusers through
#4 at connect, as they do members today. An open socket is not re-checked when the
superuser flag is removed, which is the same as when a membership is removed today.

### Settings / infrastructure

None. No env var, migration or controller change. Deploy by restarting Daphne/web and
the Celery workers: the `notify` and `assist` tasks import `get_project_access`
indirectly (#13).

### Data flow

1. A superuser opens `/all-projects/?q=acme&runner=connected`. `superuser_required`
   passes, the view binds `AllProjectsFilterForm(request.GET)`, and
   `form.filters()` produces `AllProjectsFilters`.
2. `list_all_projects(actor, filters, page, per_page)` runs 4 queries and returns a
   `Page[AllProjectsRow]`. The view adds `connected_machine_state(get_visual_engine_status())`
   and renders.
3. The superuser clicks a project, and `project_membership_required` →
   `get_project_access` (1 query) returns `ProjectAccess(role="", via_superuser=True)`.
   `can_write` is true, so the view runs and the context shows "Admin access".
4. The page's Alpine socket connects. `_get_project` → `get_project_for_user` →
   `get_project_access`, and the superuser joins the project group.
5. A POST (for example Start run) passes the decorator's write gate. The service
   records the superuser as the actor, as for any member. Notifications still go to
   the writable members only.

## Alternatives considered

- **Create real memberships for superusers (auto-join, or a "Join" button).** This
  would need no access-code change, but it pollutes member lists and the writer
  invariant, sends the superuser every project's notifications, and leaves stale rows
  after demotion. Rejected. The access fallback is computed, so it disappears the
  moment the flag is removed.
- **Check `is_superuser` in each view or decorator.** That spreads the rule across
  more than 100 views and the consumers, and it misses the Celery-side check (#13).
  Rejected in favour of the single resolver.
- **Show every project on `/projects/` for superusers.** The request asks for
  "another page", and mixing them would confuse the superuser's own work list and its
  per-row `viewer_can_write` actions. Rejected.
- **Add a new `ProjectRole.ADMIN` in `WRITABLE_ROLES`.** That role would be stored and
  would still need rows. Rejected for the same reason as auto-join.
- **Use the Django admin changelist as the "All projects" page.** It is a model-driven
  page builder, which the rules forbid as the primary implementation, and it does not
  match the design system. Rejected.

## Risks and mitigations

- **Wider blast radius of a superuser account.** A compromised superuser can now drive
  every test machine, read every Connection key, download runners with an embedded key
  and see TestRail settings. Mitigation: the superuser flag can only be set in Django
  admin or `createsuperuser` (the Users page cannot create superusers), login is
  throttled, and the `is_active` check is in the one predicate. Call this out in the
  change note.
- **Reversal of a documented decision** (user-management Q3). Update CLAUDE.md
  "Authorization" and the change note in the Document step so future agents do not
  "fix" it back.
- **Regression for normal users in `get_project_access`.** It moves from querying
  membership to querying the project with a role subquery. Non-members must still get
  `None`, and the archived filter must be kept. The verification below covers 404 and
  403 for regular and read-only users.
- **Annotation typing under mypy strict.** Use the existing narrow ignore pattern; no
  broad `Any`.
- **Query cost of `icontains` on name, email and tag** across all projects. These are
  sequential scans, but on small tables (projects number in the hundreds). Use
  `Exists` so there is no row fan-out. Revisit with trigram indexes only if needed.
- **XSS.** Project names, tags and emails render auto-escaped. No `|safe`.
- **Open redirect through `next`.** Reuse `_next_or_default`, which validates the
  host.
- **Celery side effect (#13).** A paused-help message from a superuser on a project
  where they are not a member is now sent as them instead of being dropped. This is
  intended.
- **Superuser demoted while viewing.** The next HTTP request returns 404, and open
  sockets keep streaming until they disconnect, the same as removal from a project
  today.

## Implementation steps

1. **Access core.** Add `has_superuser_project_control` and the extended
   `ProjectAccess`. Rewrite `get_project_access` as a single query with the superuser
   fallback, delegate `get_project_for_user` to it, and add `project_admin_access` to
   the context processor. (files: `projects/project_access.py`,
   `projects/project_access_services.py`, `projects/services.py`
   (`get_project_for_user` only), `projects/context_processors.py`)
2. **Member management and badge.** Make `_ensure_writer` superuser-aware, change
   `list_project_members(viewer_access=)` and update its Settings caller. Add the
   "Admin access" badge in the project layout and the five wizard templates. (files:
   `projects/project_member_services.py`, `projects/testrail_views.py`,
   `templates/layouts/project.html`, `templates/projects/wizard/step_*.html`)
3. **Test sessions.** Replace `_owner_is_member` with `_owner_has_access` and make the
   superuser case links work in `list_session_cases`. (files:
   `test_sessions/services/case_outcome.py`,
   `test_sessions/services/session_queries.py`)
4. **All projects service.** Add the types, filters and `list_all_projects` with its
   row DTO. (files: `dashboard/all_projects_types.py`,
   `dashboard/services/all_projects.py`)
5. **All projects page.** Add the form, the view, the URL, the template and the nav
   link. Steps 4 and 5 can go to one sub-agent once step 1 exists. (files:
   `dashboard/all_projects_forms.py`, `dashboard/all_projects_views.py`,
   `dashboard/urls.py`, `templates/dashboard/all_projects.html`,
   `templates/partials/nav_links.html`, `sidebar.html` / `topbar.html` (contract
   comments only), `static/css/punk-hazard.css` (only if a filter-bar class is
   missing))
6. **Archive and restore return.** Add the optional validated `next` to
   `project_archive` and `project_restore`. (files: `projects/views.py`)
7. Run `pre-commit run --all-files`.

Steps 2, 3 and 4–5 touch disjoint files and can run in parallel after step 1. Step 6
is a few lines in `projects/views.py` and should be done by whoever owns no other part
of that file.

## Verification

Manual checks (this project has no automated tests). Use a dev database with users A
(a superuser who is not a member of project P), B (a writable member of P), C (a
read-only member of P), D (a regular user who is not a member) and S (a superuser who
is a read-only member of P).

- `python manage.py check`, `mypy .` and `pre-commit run --all-files` are clean.
- **D:** `/projects/P/` returns 404, any POST to P returns 404, `/all-projects/`
  returns 403, the nav has no "All projects" link, and the P run socket is closed on
  connect.
- **C:** GET pages return 200 with no write controls, POSTs return 403, and the
  "Read-only access" badge shows.
- **B:** unchanged. Notifications, inbox and attention for P still go to B only.
- **A:**
  - `/all-projects/` lists P. Search by P's name, by B's email and by a tag finds P,
    and each filter and sort narrows or orders the list correctly.
  - Opening P shows "Admin access" and all write controls.
  - The A user starts a run, answers a person step and opens a help chat, and the
    WebSockets on the run, case, machine setup and knowledge base pages receive
    events.
  - On Settings, A can invite a user and change B's role. Demoting B, if B is the
    last writable member, is still refused. Leave is not shown.
  - A can change the run mode.
  - A receives no ntfy, inbox or attention entry for P, and P is absent from A's
    dashboard, `/failures/` and `/projects/`.
  - A test session that A started while acting on P shows live status and a working
    case link.
- **S:** has writable access on P, and the badge shows "Admin access" (not
  "Read-only").
- **A on an archived project:** the row shows Restore, Restore returns to
  `/all-projects/` with the filters kept, and before restore the project pages
  return 404.
- **Query count:** with `django.db.connection.queries` (DEBUG shell) or the Django
  debug log, `/all-projects/` makes the same number of queries for page sizes 10 and
  100 (target 5 for the service and engine status, plus session and user).
- **Toggle `is_superuser` off for A in Django admin:** P returns 404 again.
- Run the `reviewer` and `verifier` agents in parallel. Give `verifier` the verbatim
  request and this plan path.
- Document step: write `docs/changed/2026-11-01-admin-all-projects.md`, and update
  CLAUDE.md "Authorization" to say that superusers have implicit writable access to
  every project, are not members (no notifications, no invariants), and that
  `has_superuser_project_control` is the one predicate.

## Open questions

- **Q1 What exactly does "control all" cover?** Recommended: the superuser is writable
  on every project, including ones where they are a read-only member, through
  `get_project_access`. Archived projects can only be restored (not opened) until they
  are restored, as for members. (Assumption in this draft: yes, as recommended.)
- **Q2 Should a superuser's personal surfaces include projects where they are not a
  member?** These surfaces are notifications (ntfy), inbox and attention, the
  dashboard, `/failures/`, machine health, the leaderboard, `/projects/`, and Test
  Runner email attribution. Recommended: no, all stay membership-based, and the All
  projects page is the way in. Otherwise every superuser would be paged for every
  project. (Assumption: no.)
- **Q3 Should actions a superuser takes on a project where they are not a member be
  audited or labelled?** For example, "Admin" in actor labels and a log of admin
  actions. Recommended: not in this version. Actions are already attributed to the
  superuser's own name (`written_by`, actor labels, test sessions), and the "Admin
  access" badge makes the mode visible to them. A real audit log should be its own
  plan if wanted. (Assumption: no audit or labels.)
- **Q4 Are these the right search and filters?** Recommended: search over project
  name, member email and tag. Filters: status (active / archived / all), Test Runner
  (connected / disconnected), run activity (active / idle), and my membership (mine /
  not mine). Sort by newest, name or latest activity. Rows offer Open, Archive and
  Restore only. (Assumption: as listed.)

## Changelog

- 2026-11-01: Q1–Q4 answered by the user: all as recommended. Status In progress.
- 2026-11-01: created (Draft). Reverses `feat-user-management.md` Q3 (no implicit
  superuser access) for project access only.
- 2026-11-01: implemented. Deviations: `list_project_members` takes `viewer_access` in addition to `viewer` (rows need the viewer id for `is_you`); the badge uses `badge-warning` via `partials/badge_admin_access.html`; rows carry `viewer_is_member`; member counts are active users only.
- 2026-11-01: review finding rejected: "move the superuser predicate onto `CustomUser` so test_sessions does not mirror it". test_sessions may import only `projects.models`; the two mirrors carry a comment naming `has_superuser_project_control`, and moving the predicate into `accounts` is out of scope here.
