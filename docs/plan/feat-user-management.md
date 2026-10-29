# feat: User management (Users page, superuser-only run mode, read-only members)

Status: In progress
Created: 2026-10-29
Request: Four related access-control changes. (1) A superuser can list users and
create new ones from an in-app, server-rendered "Users" page. Everyone else gets
403. (2) Only a superuser may choose or change a run's help mode (Attended / Night /
Human in the loop, `RunHelpMode`) at Start, Resume, Re-run, the mid-run switch and
the chat "Re-run with redesigned plan". The rule is enforced in the service layer
and the controls are hidden for everyone else. (3) Project membership gets a role.
A **read-only** member can view everything in the project but cannot change
anything: every mutating view, form POST, WebSocket action and Test Runner person-step
answer made in their name is refused. Writable members grant and revoke read-only on the
Settings Members card, and a project always keeps at least one active writable
member. (4) Read-only members get no notifications (ntfy, night digest, inbox).

## Context

- `accounts`: `CustomUser` (`AbstractUser`, `username=None`, unique `email`,
  `USERNAME_FIELD="email"`), `CustomUserManager.create_user/create_superuser`,
  `EmailBackend`. `accounts/services.py` is a legacy 19-line module
  (`authenticate_user`, `login_user`) imported by `accounts/views.py`. There are no
  `accounts/exceptions.py` or `accounts/decorators.py`. Users can only be created in
  Django admin (`accounts/admin.py`) or `createsuperuser`.
- No email backend is configured (`auto_tester/settings.py` has no `EMAIL_*`), so
  invite or reset emails are not possible today. There is no password-change UI.
- `Project.members` is an **auto-created** M2M (`projects/migrations/0001_initial.py`):
  table `projects_project_members`, columns `id` (BigAutoField), `project_id`,
  `customuser_id`, unique (`project`, `customuser`). Reverse accessor `user.projects`.
  Latest projects migration: `0040_person_step_channel`.
- `projects/decorators.py::project_membership_required`: `login_required` plus
  `get_project_for_user` (one query, non-archived, `members=user`), 404 otherwise.
  It is used by 56 project views (`projects/views.py`, `testrail_views.py`,
  `member_views.py`, `person_step_views.py`, `runner_update_views.py`) and 14
  `machine_setup/views.py` views.
- Project views that do **not** use the decorator: `project_edit`,
  `project_archive`, `project_restore`, `project_duplicate` (`@login_required` +
  `get_project_for_user` / `get_project_by_id`), and the copy target lookup in
  `test_case_copy_to_project`.
- Membership services: `projects/project_member_services.py` (`list_project_members`,
  `invite_member`, `leave_project`, project-row lock). Members card in
  `templates/projects/settings.html`, rendered by
  `projects/testrail_views.py::render_project_settings`. Change note
  `docs/changed/2026-10-28-project-sharing.md` says "no roles".
- Help mode writes (every place `TestRun.help_mode` changes):
  - `projects/services.py::start_test_run(test_run, *, help_mode)`. Callers:
    `views.test_run_start` (`TestRunStartForm`, required radio) and
    `views.setup_run` (wizard, hard-coded `ATTENDED`).
  - `projects/run_reopen_services.py::reopen_run(..., help_mode=)` (writes the
    field). Callers: `run_resume_services._resume_pivots` (from `resume_test_run`,
    `resume_test_run_case`), `run_rerun_services.rerun_test_run_cases`,
    `case_review_redesign_services.rerun_with_redesigned_plan`, and
    `continue_paused_run` (no mode, keeps it).
  - `projects/run_help_mode_services.py::change_run_help_mode` (mid-run switch,
    `views.test_run_help_mode`).
  - View entry points: `test_run_start`, `test_run_resume`, `test_run_case_resume`,
    `test_run_cases_rerun`, `test_run_help_mode` (projects/views.py) and
    `plan_redesign_rerun` (machine_setup/views.py via
    `machine_setup/services/result_card_decision.rerun_redesign_from_chat`).
    `dashboard/services/failed_case_bulk.py` calls `rerun_test_run_cases` without a
    mode. `services.rerun_cases` (Re-run failed / blocked into a new run) copies the
    source run's mode, which is not a choice.
  - Templates: Start radio and mid-run select in `templates/projects/test_run_detail.html`;
    `templates/projects/includes/_help_mode_select.html` (included with `only` from
    `test_run_detail.html` x2, `test_run_case_detail.html`, `machine_setup/chat.html`).
- Notification recipient selection:
  - `notifications/services/recipients.py::recipient_user_ids` is the single ntfy
    recipient query for `projects/case_help_notifications.py`,
    `case_failed_notifications.py`, `person_step_notifications.py`,
    `run_finished_notifications.py` and `machine_setup/services/turn_reply_notification.py`.
  - `notifications/services/night_digest.py::_recipients_by_project`.
  - `notifications/services/record_missed_failure.py` (inbox rows for every active member).
  - `notifications/services/inbox_broadcast.py::broadcast_project_attention_changed`
    (live nudge only).
  - Inbox reads (`notifications/services/inbox.py`, `dashboard/services/attention.py`)
    are scoped by `dashboard/services/member_projects.member_project_ids`. Open help
    requests and person steps are listed live for every member.
- WebSockets. Browser consumers (`projects/consumers.py` `UploadProgressConsumer`,
  `TestRunConsumer`, `TestRunCaseConsumer`, `AgentStatusConsumer`;
  `dashboard/consumers.py::AttentionConsumer`) have **no inbound actions**.
  `machine_setup/consumers.py::MachineSetupConsumer.receive` takes only the presence
  heartbeat, which records that the member is viewing a thread. The controller
  socket (`projects/controller_consumer.py`) is authenticated by the project's
  Connection key, not a user. Its only person-facing inbound action is
  `person_step_action`, attributed via
  `projects/runner_person_step_services.resolve_runner_person` / `_current_member`
  (active member by email). `visual_engine` is global (shared token), not per project.
- Cross-project writes outside the decorator: `dashboard/failure_views.failed_cases_bulk`
  (Check / Re-run / Send to TestRail, scoped by `member_project_choices`).
- GET views that write (housekeeping, not user actions): `machine_setup.views.setup_chat`
  and `setup_thread` call `settle_stale_auto_fix_turn`, and `project_environment` →
  `runner_update_offer.get_runner_update_offer` writes a stale update `expired`.
  `test_case_import` caches TestRail picker errors. These are fine for read-only
  viewers. No GET view performs a user-intended change.
- Admin: `projects/admin.py::ProjectAdmin` has `filter_horizontal = ("tags", "members")`
  and `"members"` in `fields`. Django refuses `filter_horizontal`/form fields on an M2M
  with an explicit `through` (admin.E013), so this must change with the model.

## Goals
- Superuser-only Users page: list users, create a user with an initial password.
- Help mode chosen or changed only by superusers, enforced in the services. Non-superusers
  keep the run's stored mode, and new drafts are Attended.
- Per-membership role (`member` / `read_only`) enforced at one choke point (the
  decorator) plus the few non-decorated write paths.
- Writable members manage roles. Invite can add as read-only. Invariant: at least one
  active writable member.
- Read-only members receive no ntfy, digest or inbox items, and see no attention
  items for those projects.
- Lossless migration: every existing membership becomes writable.

## Non-goals
- Editing, deactivating or deleting users, resetting passwords, or self-service password
  change (Django admin still does these).
- Email invites or password-reset email (no email backend).
- Superusers seeing or acting on projects they are not members of (see Q3).
- Removing other members from a project (still only Leave).
- Controller protocol changes. Restricting what the Connection key can do.
- Tests (project rule).

## Design

### Data model

`projects/models.py`:

- `class ProjectRole(models.TextChoices)`: `MEMBER = "member", "Member"`,
  `READ_ONLY = "read_only", "Read-only"`. A role enum, not a boolean, so it can be
  extended later (`CLAUDE.md`: keep the user model extensible).
- `class ProjectMembership(models.Model)`:
  - `project = ForeignKey(Project, on_delete=CASCADE, related_name="memberships")`
  - `user = ForeignKey(settings.AUTH_USER_MODEL, on_delete=CASCADE,
    related_name="project_memberships", db_column="customuser_id")`
  - `role = CharField(max_length=16, choices=ProjectRole.choices, default=ProjectRole.MEMBER)`
  - `Meta: db_table = "projects_project_members"`,
    `unique_together = [("project", "user")]`
  - `id` is the default `BigAutoField`, which matches the existing column.
- `Project.members = ManyToManyField(AUTH_USER_MODEL, through="ProjectMembership",
  blank=True, related_name="projects")`. The related name stays `projects`, so every
  existing `projects__id=` / `members=user` query keeps working.
  `project.members.add(user)` / `.remove(user)` keep working (through model with
  defaulted extra fields, Django ≥ 2.2), and add rows with role `member`.

**Migration `projects/0041_project_membership.py` (hand-written; do not let
`makemigrations` generate it, because the autodetector would drop and recreate the table and
lose every membership):**

```
operations = [
  SeparateDatabaseAndState(
    database_operations=[],                      # table, columns, FK indexes and the
                                                 # unique pair already exist
    state_operations=[
      CreateModel("ProjectMembership", id BigAutoField, project FK, user FK
                  (db_column="customuser_id"),
                  options={"db_table": "projects_project_members",
                           "unique_together": {("project", "user")}}),
      AlterField("project", "members", ManyToManyField(through="projects.ProjectMembership",
                 blank=True, related_name="projects", to=AUTH_USER_MODEL)),
    ]),
  AddField("projectmembership", "role", CharField(default="member", max_length=16, choices=...)),
]
```

- This follows Django's documented recipe ("Changing a ManyToManyField to use a
  through model", docs 5.2). It differs on purpose by **keeping** the table name through
  `db_table` and the column through `db_column`, so `database_operations` is
  empty and no raw SQL is needed. The `unique_together` in the state is matched by
  the existing unique index. Django finds it by introspection if it ever drops it, so
  the stored constraint name does not matter. This is why we use `unique_together` and
  not a named `UniqueConstraint`, which would be declared under a name that does not
  exist in the database.
- `AddField role` with a constant default: Postgres 11+ adds the column without a
  table rewrite and fills existing rows with `'member'`. Every current member
  becomes writable. No data migration, no data loss.
- Before writing the migration, the implementer confirms on the dev DB (read-only:
  `python manage.py sqlmigrate projects 0001`, or `\d projects_project_members` in
  `dbshell`) that the table and column names and the `id` type are as listed above.
  After writing it: `python manage.py sqlmigrate projects 0041` must print **only** the
  `ADD COLUMN "role"` statements, and `makemigrations --check --dry-run` must report no
  changes.
- Reverse: drops the `role` column (role data lost, memberships kept) and restores
  the auto M2M state. Safe.
- Parallel worktree risk: the "test sessions" session may also add `projects/0041_*`.
  Whoever merges second renumbers its migration (or adds a merge migration) and
  re-checks `makemigrations --check`.

### Services

**New `projects/project_access.py`** (leaf module, no ORM):
- `@dataclass(frozen=True) class ProjectAccess: project: Project; role: str;
  can_write: bool` (`can_write = role == ProjectRole.MEMBER`).
- `set_request_project_access(request: HttpRequest, access: ProjectAccess) -> None`
  and `get_request_project_access(request: HttpRequest) -> ProjectAccess | None`.
  These store and read one private request attribute with an `isinstance` check, so the
  context processor can read what the decorator resolved without another query.

**New `projects/project_access_services.py`:**
- `get_project_access(project_id: int, user: CustomUser, *, include_archived: bool = False)
  -> ProjectAccess | None`. One query:
  `ProjectMembership.objects.select_related("project").filter(project_id=…, user=…,
  [project__archived=False]).first()`. This replaces `get_project_for_user` in the
  decorator at the same query count (1). `get_project_for_user` / `get_project_by_id` stay
  for consumers and other callers.

**`projects/decorators.py`:**
- `project_membership_required` (unchanged name and signature, so no change at the 70 call
  sites) resolves `get_project_access`: 404 if none, as today. If the request method
  is not safe (`request.method not in ("GET", "HEAD", "OPTIONS")`) and
  `not access.can_write`, it raises `PermissionDenied` (403). Otherwise it stores the
  access on the request and calls the view with `project=access.project`.
- New `project_read_access_required` (same signature) does not apply the method gate. It is
  used **only** by `member_views.project_leave` (a read-only member may leave, see Q5).
  This is the explicit opt-out. Any future opt-out must be a deliberate change of
  decorator.
- Why one choke point and not a check in every service: all 70 project views already go
  through the decorator, and every one that mutates is POST-only (audited above).
  A method gate covers new views automatically.

**New `projects/context_processors.py::project_access(request) -> dict[str, object]`:**
returns `{"project_can_write": access.can_write, "project_role": access.role}` when the
decorator stored access, else `{}`. Zero queries. Registered in `settings.py`
`TEMPLATES[...]["context_processors"]` (one line).

**`projects/project_member_services.py`** (extend):
- `ProjectMemberRow` gains `role: str`, `role_label: str`, `can_write: bool`.
  `list_project_members` reads the through rows:
  `ProjectMembership.objects.filter(project=…).select_related("user").only(...)`,
  still one query. `ProjectMembers` gains `viewer_can_write: bool`. `can_leave` becomes:
  a read-only viewer can always leave, and a writable viewer only if another
  **active writable** member remains.
- `invite_member(..., role: str = ProjectRole.MEMBER)`: `project.members.add(user,
  through_defaults={"role": role})`. The validation is unchanged.
- `leave_project`: under the existing project lock, if the leaving member is writable
  and no other active writable member remains, raise `LastWritableMemberError`.
  A read-only member can always leave.
- New `change_member_role(*, project, actor: CustomUser, user_id: int, role: str) -> None`:
  inside a transaction with the project lock. It re-checks that `actor` is a writable member
  (defence in depth, one query). Then the target membership must exist (else
  `NotProjectMemberError`). Demoting the last active writable member raises
  `LastWritableMemberError`. A change to the same role is a no-op. A member may
  demote themselves if another active writable member remains.

**`projects/exceptions.py`** (append):
- `LastWritableMemberError(ProjectMembershipError)`: "A project needs at least one
  active member who can make changes."
- `InvalidMemberRoleError(ProjectMembershipError)` (unknown role value; the form
  normally prevents it).
- `HelpModeNotAllowedError(Exception)` with `.message`: "Only a superuser can choose
  or change the run mode."

**New `projects/run_help_mode_policy.py`** (leaf):
- `resolve_help_mode(*, user: CustomUser, requested: str | None, current: str) -> str | None`.
  It returns the value to store, or `None` to keep `current`. A superuser gets `requested`.
  For a non-superuser, `None` or `requested == current` gives `None`; anything else
  raises `HelpModeNotAllowedError`. A forged POST is refused, not silently ignored,
  so the person sees why. Validation of unknown values (`is_valid_help_mode`) stays
  where it is and runs first.
- Every help-mode-writing service calls it **after taking the run lock and before any
  write**, against the locked `help_mode`:
  - `services.start_test_run(test_run, *, user: CustomUser, help_mode: str | None)`.
    It reads `status, help_mode` under the existing lock. `None` keeps the stored mode
    (Attended for every new draft). `views.setup_run` passes `user` and keeps
    `ATTENDED`, which equals the new draft's stored mode, so non-superusers pass.
  - `run_resume_services._resume_pivots` (already has `user`).
  - `run_rerun_services.rerun_test_run_cases` (already has `user`).
    `dashboard/services/failed_case_bulk.py` passes `help_mode=None`, so it is unaffected.
  - `case_review_redesign_services.rerun_with_redesigned_plan` (already has `user`).
  - `run_help_mode_services.change_run_help_mode`: a non-superuser is refused before
    the lock (`HelpModeNotAllowedError`), whatever the value.
  - `reopen_run` itself stays policy-free (it has no user). Its callers resolve first.
- `continue_paused_run`, `reset_test_run` and `rerun_cases` do not change the mode. No change.

**New `accounts/user_management_services.py`** (sibling module, not a package):
- Why a sibling: `accounts/services.py` and an `accounts/services/` package cannot
  coexist (the package shadows the module and breaks `accounts.views`'s import).
  Converting the module into a package would rename a file the parallel "test
  sessions" worktree may edit. `projects` already uses flat `<use_case>_services.py`
  modules next to its legacy `services.py`, so this follows the existing convention
  with zero import churn.
- `@dataclass(frozen=True) UserRow(id, email, full_name, is_superuser, is_active,
  date_joined, last_login, project_count)`.
- `list_users(*, page: int, per_page: int) -> Page[UserRow]`: one query
  (`annotate(project_count=Count("project_memberships"))`, `order_by("email")`,
  `only(...)`) plus the paginator count.
- `create_user(*, actor: CustomUser, email: str, first_name: str, last_name: str,
  password: str) -> CustomUser`. It refuses a non-superuser actor
  (`SuperuserRequiredError`, defence in depth). It refuses an email that already
  exists case-insensitively (`UserEmailTakenError`): `EmailBackend` looks up exactly,
  so near-duplicates would confuse login and invites. It creates the user with
  `CustomUser.objects.create_user(...)` (`is_staff=False`, `is_superuser=False`,
  active) inside `transaction.atomic()`. `IntegrityError` on the unique email is
  mapped to `UserEmailTakenError` for the race. Never log the password.

**New `accounts/exceptions.py`:** `UserManagementError(Exception)` with `.message`;
`UserEmailTakenError`, `SuperuserRequiredError` subclasses.

**New `accounts/decorators.py`:** `superuser_required(view)`: `login_required`, then
`PermissionDenied` (403) unless `request.user.is_superuser` (and active).

### Pages and forms

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `accounts:users` `/accounts/users/` | GET | `superuser_required` (403 otherwise) | `UserCreateForm()` | `accounts/users.html`: `users` (Page[UserRow]), `form`, pagination keys as in other lists, `active_nav="users"` | — |
| `accounts:user_create` `/accounts/users/create/` | POST | `superuser_required` | `UserCreateForm` (email, first_name, last_name optional, password1, password2; `clean()` runs `validate_password` with a transient `CustomUser` for similarity checks) | Invalid input re-renders `accounts/users.html` with the bound form and the list. Success → PRG redirect to `accounts:users` + success message "Created <email>." | `UserEmailTakenError` → form error on `email`; password errors on `password2` |
| `projects:member_role` `<id>/settings/members/<int:user_id>/role/` | POST | `project_membership_required` (writable) | `ProjectMemberRoleForm` (`role` ChoiceField over `ProjectRole`) | redirect to `projects:settings` + message | `ProjectMembershipError.message` → `messages.error` |
| `projects:member_invite` (existing) | POST | writable | `ProjectMemberInviteForm` + `read_only` BooleanField | unchanged | unchanged |
| `projects:leave` (existing) | POST | **`project_read_access_required`** | — | unchanged | `LastWritableMemberError` |
| every other `project_membership_required` POST (69 views) | POST | writable, else 403 | unchanged | unchanged | 403 page |
| `projects:edit`, `archive`, `restore`, `duplicate` | POST | `@login_required` + `get_project_access(...)`. `can_write` is required, else 403 (`restore` uses `include_archived=True`) | unchanged | unchanged | 404 non-member, 403 read-only |
| `projects:test_case_copy_to_project` | POST | source writable (decorator) **and** target writable (`get_project_access(target).can_write`, else the existing "not found" path) | unchanged | unchanged | |
| `dashboard:failed_cases_bulk` | POST | `@login_required`. Scope = **writable** member projects | unchanged | unchanged | read-only cases count as "skipped" |

Help mode forms (`projects/forms.py`):
- `TestRunStartForm.help_mode`: `required=False`. `clean_help_mode` returns `None` for
  blank (as `RunHelpModeForm`). Superusers' radio always posts a value. Non-superusers'
  page posts none.
- `RunHelpModeForm`, `RunHelpModeChangeForm`: unchanged. The service policy
  decides.
- Views add `HelpModeNotAllowedError` to their `except` tuples and show
  `exc.message`: `test_run_start` (alongside `ValueError`), `test_run_resume`,
  `test_run_case_resume`, `test_run_cases_rerun`, `test_run_help_mode`,
  `machine_setup.views.plan_redesign_rerun`. Each view passes `request.user` where it
  does not already.

Template changes (all server-side `{% if %}`, so nothing is sent to the client):
- `_help_mode_select.html` gets a `can_change` parameter (callers pass
  `can_change=user.is_superuser`; it is included with `only`). When false it renders
  "Run mode: <label>" text and no `<select>`, so the blank value keeps the stored mode.
- `test_run_detail.html`: the Start dialog radio and the mid-run switch are shown only for
  `user.is_superuser`. Others see the current mode as text.
- Every POST form or button in project-scoped templates is wrapped in
  `{% if project_can_write %}`. Files with POST forms:
  `projects/test_run_detail.html`, `test_run_case_detail.html`, `settings.html`,
  `application_context.html`, `testrail_suite_picker.html`, `wizard/step_details.html`,
  `wizard/step_context.html`, `wizard/step_run.html`, includes `_test_case_bulk_dialogs`,
  `_run_help_banner`, `_project_dialogs`, `_case_help_card`, `_upload_delete_dialog`,
  `_testrail_run_import_card`, `_testrail_push_dialog`, `_testrail_import_card`,
  `_test_run_delete_dialog`, `_test_case_edit_dialog`, `_test_case_delete_dialog`,
  `_test_case_create_dialog`, `_person_step_card`, `_import_history_table`,
  `_environment_connection`, `partials/runner_download_card.html`,
  `partials/confirm_dialog.html` (callers), `machine_setup/chat.html` and includes
  `_run_proposal`, `_help_panel`, `_composer`, `_result_change`, `_plan_redesign`.
  The trigger buttons that open those dialogs go too: test case "New / Edit / Delete",
  run "Start / Stop / Delete / Reset / Re-run / Resume", "Ask the agent",
  "Refine" (fetch POST in `step_context.html` / `application_context.html`), etc.
  For an include with `only`, pass `can_write=project_can_write`
  explicitly. The implementer greps `only %}` on includes that render forms.
- Read-only members see a small "Read-only access" badge in the project page header
  (`layouts/project.html`, via `project_can_write`), so missing buttons are explained.
- Connection key card (`partials/connection_key_card.html`), regenerate, runner
  download card and the wizard's machine step are hidden for read-only members (see Q6).
  The Environment page still shows the machine status.
- `projects/list.html` / `_project_table.html`: Edit / Archive / Restore / Duplicate
  row actions only when `project.viewer_can_write`. This is a new `Exists(...)` annotation
  in `services.list_projects_for_user` (one subquery in the existing query, no N+1).
- Settings Members card: a Role column (badge "Read-only"). For writable viewers there is a
  per-row role `<select>` + Save posting to `projects:member_role`, and the invite form gets a
  "Read-only" checkbox. Read-only viewers see the table and the Leave button only.
- `partials/nav_links.html`: "Users" link when the user is a superuser.
  `sidebar.html` / `topbar.html` pass `is_superuser=user.is_superuser` into the
  `only` include. New `active_nav="users"`.
- Styles: reuse `.data-table`, `.badge-*`, `.card`, `.btn-*`, `.input`. Add any new
  class to `static/css/punk-hazard.css` (likely none, or one `.badge-readonly`).

### Background work and WebSockets

- No new Celery task, queue, consumer, event or controller action.
- Browser consumers: no inbound actions besides presence (`MachineSetupConsumer`),
  which only marks "viewing" and changes no project data. It is allowed for read-only
  members. Read-only members keep receiving all read events. Membership is still checked at
  `connect()` only, so a demoted member's open socket stays read-only by nature.
- Controller `person_step_action`: `resolve_runner_person` and `_current_member`
  filter `project_memberships__project_id=…, project_memberships__role=member` (one
  join, same `filter()` call). A runner whose `CONTROLLER_PERSON_EMAIL` belongs to a
  read-only member (or someone demoted since the handshake) is treated as an
  **anonymous** runner answer ("a person at the Test Runner"). The authority still
  comes from the Connection key, as accepted in
  `docs/changed/2026-10-28-hitl-runner-window.md`, but the answer is never attributed to a
  read-only member (see Q7). No protocol change, so `CLIENT_VERSION` and capabilities
  are unchanged.

### Notifications

Recipients are filtered in the query (single join on the membership row, both
conditions in one `filter()` call so they hit the same row):
- `notifications/services/recipients.py::recipient_user_ids`: add
  `user__project_memberships__project_id=project_id,
  user__project_memberships__role=ProjectRole.MEMBER` (replacing `user__projects__id`).
  This covers Need help, Failed, person step "Your turn", run finished and chat replies.
- `notifications/services/night_digest.py::_recipients_by_project`: same join, and
  `values_list("user_id", "user__project_memberships__project_id")`.
- `notifications/services/record_missed_failure.py`: member ids via
  `CustomUser.objects.filter(project_memberships__project_id=…,
  project_memberships__role=member, is_active=True)`.
- `notifications/services/inbox_broadcast.py::broadcast_project_attention_changed`:
  same filter. This is not a notification, but it saves pointless sends.
- `dashboard/services/member_projects.py`: add `writable_member_project_ids(user)`
  (one query on `ProjectMembership`). `dashboard/services/attention.py`
  (`get_attention`, `count_attention`) and `dashboard_data.get_dashboard_data`'s
  attention use it, so help requests, person steps and missed items of read-only projects
  are not "your attention". The failed-case widget, the Failures page and Machine health keep
  `member_project_ids` / `member_project_choices` (read).
- Existing `MissedFailure` rows of a member later demoted stay until dismissed or
  resolved. This is acceptable and noted.

### Settings / infrastructure
- `settings.py`: one context processor line. No new env var, so `example.env` is
  unchanged.
- Deploy: `migrate`, restart Daphne/web and all Celery workers (the notify worker runs
  the recipient queries). No runner update.

### Data flow
- Read-only member POSTs anything under `/projects/<id>/…` →
  `project_membership_required` → `get_project_access` (1 query) → unsafe method and
  `can_write=False` → 403. The view and service never run.
- Writable member opens Settings → `render_project_settings` → `list_project_members`
  (1 query) → role selects → POST `member_role` → `change_member_role` (lock, invariant)
  → redirect + message.
- Non-superuser presses Start → no `help_mode` posted → `start_test_run(user, None)` →
  under lock, `resolve_help_mode` → keep stored mode → run starts. A forged value is refused
  with `HelpModeNotAllowedError` → message, no write.
- Case needs help → `notify_case_needs_help` → `recipient_user_ids` returns only
  writable members with the preference → `send_notification`.
- Superuser → `/accounts/users/` → `list_users` → POST create → `UserCreateForm` →
  `create_user` → PRG.

## Alternatives considered
- **`can_write` boolean** instead of a role enum: simpler, but the next role (owner,
  viewer-without-logs, ...) would need another migration and rename. A two-value enum costs
  the same.
- **Separate `ProjectMembership` table plus data copy** (new table, copy rows, drop the
  old M2M): a real data migration on a populated table, with a window where queries hit the
  wrong table. Keeping the existing table through `db_table`/`db_column` makes the change
  state-only.
- **Per-service role checks** instead of the decorator gate: ~70 services would need an
  `actor` and a check, and a new view would be open by default. The decorator gate is
  closed by default. Services stay role-agnostic, except membership/role changes, which
  re-check the actor.
- **Silently ignoring a non-superuser's help mode**: hides forged or stale-page
  attempts. Refusing with a message is clearer. The UI never posts one for them.
- **Converting `accounts/services.py` into a package**: correct long-term (matches
  `agents/services/`), but it rewrites a file the parallel worktree may touch, for no
  functional gain now. A follow-up refactor plan can do it.

## Risks and mitigations
- **Migration loses memberships if auto-generated.** The migration must be
  hand-written as above. Verify with `sqlmigrate projects 0041` (only `ADD COLUMN`),
  `makemigrations --check`, and a throwaway-DB smoke that counts memberships before and
  after `migrate` and after `migrate projects 0040` (reverse).
- **State/DB mismatch** (wrong `db_column`, `id` type, unique pair): confirmed by
  read-only introspection first. Any later `AlterUniqueTogether` relies on
  introspection, which works with the existing index name.
- **Admin check failure (E013).** Replace `filter_horizontal`/`fields` "members"
  with a `ProjectMembershipInline` (`TabularInline[ProjectMembership, Project]`,
  fields user + role, `autocomplete_fields=("user",)` since `CustomUserAdmin` has
  `search_fields`). `manage.py check` proves it.
- **Missed write path.** Grep-audit list above. The reviewer re-greps
  `@login_required` views taking a project id, `get_project_for_user(` callers and
  every `receive(` in consumers. A new view is closed by default.
- **Authorization regression for writers.** The decorator still makes 1 query. GET is
  untouched. Smoke both roles on every POST family.
- **Lock-out**: the invariant is enforced under the project lock in leave and role change.
  Deactivating a user in admin can still leave a project with no active writable
  member. This is pre-existing (true of "active member" today), and admin can fix it.
- **XSS**: user emails and names render escaped (no `|safe`). Role labels come from
  `ProjectRole`.
- **Password handling**: validators enforced, passwords never logged or put in
  messages. The success message names only the email.
- **Connection key escalation**: a read-only member who already copied the key keeps
  runner power (including answering person steps anonymously). Mitigation: hide the key
  from read-only members. The writable members can Regenerate it when demoting someone who
  had it (note in the UI copy next to the role select?). See Q6.
- **Parallel worktree conflicts**: shared files touched minimally (`accounts/urls.py`
  +2 paths, `accounts/forms.py` +1 class, `projects/models.py` +2 classes and 1 field
  edit, `settings.py` +1 line, `nav_links.html` +1 link). Migration number collision
  is handled at merge.
- **Stale pages after demotion**: buttons remain until reload. POST returns 403.
  Acceptable.

## Implementation steps

Step 1 is shared code, written first by the orchestrator. Steps 2–6 touch disjoint
files and can run as parallel sub-agents. Step 7 integrates.

1. **Shared foundation** (orchestrator). Files: `projects/models.py`
   (`ProjectRole`, `ProjectMembership`, `Project.members` through),
   `projects/migrations/0041_project_membership.py` (hand-written),
   `projects/admin.py` (membership inline), `projects/exceptions.py`
   (`LastWritableMemberError`, `InvalidMemberRoleError`, `HelpModeNotAllowedError`),
   `projects/project_access.py`, `projects/project_access_services.py`,
   `projects/decorators.py` (method gate + `project_read_access_required`),
   `projects/context_processors.py`, `auto_tester/settings.py` (register),
   `projects/run_help_mode_policy.py`, `accounts/exceptions.py`,
   `accounts/decorators.py`. Gate: `pre-commit run --all-files`,
   `manage.py check`, `makemigrations --check`, `sqlmigrate projects 0041`.
2. **Users page** (sub-agent A). Files: `accounts/user_management_services.py`,
   `accounts/forms.py` (`UserCreateForm`), new `accounts/user_views.py`
   (`users`, `user_create`), `accounts/urls.py`, `templates/accounts/users.html`,
   `templates/partials/nav_links.html`, `sidebar.html`, `topbar.html`,
   `static/css/punk-hazard.css` (only if a class is needed).
3. **Help mode policy in services** (sub-agent B). Files: `projects/services.py`
   (`start_test_run` signature and policy, `list_projects_for_user` `viewer_can_write`
   annotation, `list_other_projects_for_user` writable targets only),
   `projects/run_resume_services.py`, `projects/run_rerun_services.py`,
   `projects/case_review_redesign_services.py`, `projects/run_help_mode_services.py`.
   Interface: `start_test_run(test_run, *, user, help_mode: str | None)`.
4. **Project views, forms and membership** (sub-agent C). Files: `projects/views.py`
   (start passes user; except tuples; `project_edit/archive/restore/duplicate` via
   `get_project_access` + 403; copy-target writable check; `setup_run` passes user),
   `projects/forms.py` (`TestRunStartForm` optional; `ProjectMemberInviteForm.read_only`;
   `ProjectMemberRoleForm`), `projects/project_member_services.py`
   (roles, invariant, `change_member_role`), `projects/member_views.py`
   (`member_role`, invite role, leave decorator), `projects/urls.py`
   (`member_role`), `projects/testrail_views.py` (`render_project_settings` adds
   `role_form`/`viewer_can_write`), `machine_setup/views.py` (`plan_redesign_rerun`
   except tuple). Depends on step 3's signature.
5. **Recipients, attention, runner, bulk** (sub-agent D). Files:
   `notifications/services/recipients.py`, `night_digest.py`,
   `record_missed_failure.py`, `inbox_broadcast.py`,
   `dashboard/services/member_projects.py` (`writable_member_project_ids`,
   `writable_member_project_choices`), `dashboard/services/attention.py`,
   `dashboard/services/dashboard_data.py`, `dashboard/failure_views.py`
   (bulk scope), `projects/runner_person_step_services.py`.
6. **Templates** (sub-agent E). Files: every project and machine-setup template listed
   under "Template changes", `templates/layouts/project.html` (read-only badge),
   `templates/projects/includes/_help_mode_select.html`,
   `templates/projects/includes/_project_table.html`,
   `templates/partials/connection_key_card.html`, `runner_download_card.html`,
   `templates/projects/settings.html`. Context contract it consumes:
   `project_can_write`, `user.is_superuser`, `project.viewer_can_write` (list),
   members rows `role`/`role_label`/`can_write`/`user_id`, `viewer_can_write`,
   `role_form` choices, invite form field `read_only`, URL `projects:member_role`.
   Update template header comments (context contracts).
7. **Integrate and document** (orchestrator): wire imports and URLs, then run
   `pre-commit run --all-files`, `manage.py check`, `makemigrations --check`, and the
   smokes below. Update `CLAUDE.md` (Authorization paragraph: roles, superuser-only
   run mode, Users page) and write `docs/changed/2026-10-29-user-management.md`.

## Verification
- `pre-commit run --all-files` (isort, black, strict mypy), `python manage.py check`,
  `python manage.py makemigrations --check --dry-run`, `python manage.py sqlmigrate
  projects 0041` (only `ADD COLUMN role`).
- Throwaway-DB smoke scripts (rolled back, or on a scratch database), using the Django
  test `Client`:
  1. Migration: create memberships on 0040, migrate to 0041, assert the count is unchanged and
     every role is `member`. Migrate back to 0040 and assert the count is unchanged.
  2. Decorator: for a read-only member, GET of overview/runs/case/settings/environment/
     machine setup is 200. POST to one view of every family (test case create, run start,
     abort, resume, help continue, person report, machine setup send, TestRail import,
     settings save, invite, regenerate key, download runner, runner update, refine prompt)
     is 403, with no row changed. Leave is allowed. The same POSTs as a writer behave as
     before. Non-member is 404.
  3. Non-decorated paths: `project_edit/archive/restore/duplicate` read-only → 403.
     Copy to a read-only target is refused. Failures bulk skips read-only projects.
  4. Roles: demote the last active writer → `LastWritableMemberError`. A writer leaves when
     the only others are read-only → refused. Read-only leave → OK. Invite as read-only
     → role stored. A read-only actor calling `change_member_role` → refused.
  5. Help mode: a non-superuser starts with no value → stored mode. With a different value →
     `HelpModeNotAllowedError`, run still WAITING. Resume / case resume / rerun selected /
     redesign rerun with a different value → refused. The mid-run switch → refused.
     A superuser can do all of them. Wizard start as non-superuser → OK.
  6. Notifications: `recipient_user_ids`, `_recipients_by_project`,
     `record_missed_failure` exclude a read-only member with the preference on.
     `count_attention` for a read-only member ignores that project's help requests.
  7. Runner: `resolve_runner_person` with a read-only member's email → `None`.
  8. Users page: non-superuser GET/POST → 403. Anonymous → login redirect. A superuser
     creates a user (case-insensitive duplicate refused, weak password refused), the new
     user can log in, and the list renders with `project_count`. Assert the query count of
     the list page.
  9. Template compile: render each touched template for both roles and check no
     `<form method="post"` appears in read-only renders of project pages (except Leave).
- Manual (browser, unique port): both roles through the main pages. Superuser and
  non-superuser Start/Resume dialogs.
- Run the `reviewer` and `verifier` agents in parallel on the diff (verifier gets this
  request and plan path).

## Open questions
- **Q1 Who grants and revokes read-only?** Proposal: any writable member, on the Settings
  Members card. Superusers have no extra power there unless they are members.
  (Assumption in this draft: writable members only.)
- **Q2 How does a new user get in?** No email backend is configured, so the superuser
  sets the initial password in the form and passes it on out of band. There is no
  self-service password change yet. (Assumption: password set by the superuser.
  Invite or reset email is a non-goal and would need `EMAIL_*` settings.)
- **Q3 Do superusers implicitly see and act on all projects?** Proposal: no. Superuser only
  unlocks the Users page and the run mode. Project access stays membership-based, so
  dashboards, notifications and the "at least one writer" rule keep one meaning. This
  means a project with no superuser member can only run Attended. (Assumption: no implicit
  access.)
- **Q4 Can the Users form create superusers or staff?** Proposal: no. New users are
  regular, and promotion stays in Django admin, which keeps privilege escalation off the
  new page. (Assumption: regular users only.)
- **Q5 May a read-only member Leave?** Proposal: yes. It removes access only and cannot
  break the writer invariant. (Assumption: yes, via `project_read_access_required`.)
- **Q6 May read-only members see the Connection key and download the Test Runner?**
  The key gives runner-level power (drive the machine, answer person steps). Proposal: no.
  Hide the key card, Regenerate and the download card, and refuse the download POST (it is a
  POST, so the gate already refuses it). (Assumption: hidden.)
- **Q7 Test Runner email of a read-only member**: treat the answer as anonymous
  ("a person at the Test Runner"), or refuse every runner answer while that email is set?
  Proposal: anonymous, consistent with "anyone with the key can answer".
  (Assumption: anonymous.)
- **Q8 Does a superuser's flag override a read-only role on a project they belong to?**
  Proposal: no, the role applies to everyone. (Assumption: no override.)
- **Q9 Non-superuser Start of a run whose stored mode is Night or HITL** (for example a run a
  superuser ran, then Reset): keep the stored mode (proposal) or force Attended?
  (Assumption: keep the stored mode, since only superusers change it.)
- **Q10 May read-only members Duplicate the project or copy test cases out of it into a
  project they can write?** Neither changes the source. Proposal: no for now. Every
  project-scoped POST is closed. (Assumption: refused.)

## Changelog
- 2026-10-29: created (Draft).
- 2026-10-29: user accepted every proposed default for Q1–Q10; status In progress.
- 2026-10-29: review round 1. Fixed: `WRITABLE_ROLES` single source for "can write"; list-row access helper moved to `projects/decorators.get_writable_project`; dead `get_project_by_id` removed; `invite_member` re-checks the inviter is writable (`MemberManagementNotAllowedError`, also used by `change_member_role` instead of Django's `PermissionDenied`); run-mode text shown once on a draft run; `test_case_import` skips the TestRail picker call for read-only viewers. Rejected: Failures-page bulk bar still offered on read-only projects' rows (service skips them and says why; per-row writable data would cost an extra query for a UI-only gain); Django admin membership inline can demote/remove the last writer (superuser-only surface, same as deactivating a user in admin today; noted in the change note); `record_missed_reply` row for a sender demoted mid-turn (never shown: inbox reads writable projects only).
