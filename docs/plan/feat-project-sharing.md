# feat: Project sharing (members, invite, leave)

Status: Done
Created: 2026-10-27
Request: Let a project be shared by several users. Every member gets the
project's notifications and can manage everything in it. The project Settings
tab lists all members and has a field to invite another user by their
identifier (the user said "username"). The invite adds them at once. Any member
can leave the project, but only if at least one other member stays on it.

## Context
- Data model already supports sharing: `projects.models.Project.members` is an
  M2M to `accounts.CustomUser` (`related_name="projects"`). `create_project` and
  `duplicate_project` (`projects/services.py`) add the creator as the only member.
  Today nothing in the UI adds a second member; only Django admin can
  (`projects/admin.py`, `filter_horizontal = ("tags", "members")`).
- **There is no username.** `accounts/models.py` `CustomUser` sets
  `username = None` and `USERNAME_FIELD = "email"` (unique). Users log in with
  email (`accounts/backends.py` `EmailBackend`). There is no sign-up page, so
  accounts are created by an admin. `first_name` / `last_name` come from
  `AbstractUser` and are usually empty. `__str__` returns the email.
- Authorization is member-equal everywhere. `@project_membership_required`
  (`projects/decorators.py`) → `get_project_for_user` (non-archived, `members=user`).
  The edit, archive, restore and duplicate views (`projects/views.py`) and every
  `machine_setup` view use the same membership lookup. `grep` finds no owner,
  creator or first-member check anywhere (`SetupConversation.created_by` is only
  provenance, nullable, `SET_NULL`). So "can manage everything" already holds:
  any member can regenerate the connection key, archive the project, change
  TestRail settings, start and abort runs, and so on. No change is needed.
- Notifications already reach every member.
  `notifications/services/recipients.py` `recipient_user_ids` filters
  `NotificationPreference` by `user__projects__id=project_id`. It is used by
  `projects/case_help_notifications.py`, `projects/case_failed_notifications.py`
  and `machine_setup/services/turn_reply_notification.py` (the last narrows to
  the turn's sender with `among=`). A new member gets notifications once they
  turn on their own preference at `/notifications/`. A member who left drops
  out automatically, because the membership join no longer matches.
- Project list: `list_projects_for_user` already annotates `member_count`, and
  `templates/projects/includes/_project_table.html` shows a Members column.
- Settings page: `projects:settings` → `projects/testrail_views.py`
  `project_settings` / `_render_settings_page` → `templates/projects/settings.html`
  (one TestRail card). Its context: `project`, `form`, `api_key_hint`,
  `active_tab`, `active_nav`.
- WebSockets: `projects/consumers.py` `AuthenticatedConsumer` and
  `machine_setup/consumers.py` `MachineSetupConsumer` check membership only in
  `connect()` (`get_project_for_user`). Client frames are read-only (presence);
  every write goes through a membership-checked view.
- Throttling: no general mechanism. `notifications/services/test_notification.py`
  throttles with the Redis cache (`cache.add` with a timeout, django-redis
  `CACHES["default"]`) and raises `TestNotificationThrottledError`. Login is
  not throttled today. That is out of scope here, but worth knowing.
- Service layout: `projects/services.py` is the legacy module. `projects/` holds
  leaf use-case modules named `<thing>_services.py` (`run_rerun_services.py`,
  `case_help_services.py`, ...). A `projects/services/` package cannot exist next
  to `services.py`, so a leaf module is the convention to follow.

## Goals
- Settings tab shows a **Members** card that lists every member (email, full
  name if set, "You" badge, "Inactive" badge for a deactivated account).
- Invite by exact email: the user is added at once, with no acceptance step.
- Any member can leave the project, unless no other active member would remain.
- Errors shown inline on the invite field. Leave is refused with a message.

## Non-goals
- Removing other members (not requested; see Q2).
- Roles or owners: all members stay equal.
- Invitations to people without an account, email invites, an acceptance flow,
  or a notification to the invitee.
- A `username` field on the user model (see Q1).
- Cutting off a leaver's already-open WebSocket streams (see Risks).
- Leaving an archived project (Settings is not reachable for archived projects).
- Login throttling (an existing gap; it needs its own plan).

## Design

### Data model
No changes and no migration. `Project.members` is used as it is.

### Services
New leaf module **`projects/project_member_services.py`**. It holds one
cohesive responsibility, project membership, with three use cases:

- `list_project_members(project: Project, *, viewer: CustomUser) -> ProjectMembers`
  - Frozen dataclasses: `ProjectMemberRow(user_id, email, full_name, is_you,
    is_active)` and `ProjectMembers(rows: list[ProjectMemberRow], can_leave: bool)`.
  - **One query**: `project.members.only("id", "email", "first_name",
    "last_name", "is_active").order_by("email")`. `can_leave` is computed from
    the rows: at least one other row with `is_active`. Members per project are
    few, so no pagination.
- `invite_member(*, project: Project, inviter: CustomUser, email: str) -> CustomUser`
  1. Throttle first (before any lookup): a per-inviter counter in the default
     cache, key `projects:member-invite:<inviter_id>`. `cache.add(key, 0,
     timeout=INVITE_WINDOW_SECONDS)` then `cache.incr(key)`. If the key expired
     in between, `incr` raises `ValueError`; treat that as count 1. Above
     `INVITE_LIMIT` raise `MemberInviteThrottledError`. These are module
     constants (10 attempts per 600 s), like `TEST_NOTIFICATION_WINDOW_SECONDS`,
     not env settings. Every attempt counts, including successful ones.
  2. Find the user: `CustomUser.objects.filter(email__iexact=email.strip(),
     is_active=True)`. If more than one row matches (emails are unique only by
     exact case), use the exact match and fall back to not found. No user →
     `MemberNotFoundError`. Unknown and inactive accounts get the **same**
     error, so the answer reveals only that an active account exists.
  3. `transaction.atomic()`: `Project.objects.select_for_update().only("id")
     .get(pk=project.pk)`. Then `project.members.filter(pk=user.pk).exists()` →
     `AlreadyMemberError`. Otherwise `project.members.add(user)`.
  - The project row lock is the same one `leave_project` takes, so an invite
    and a concurrent leave are serialized.
- `leave_project(*, project: Project, user: CustomUser) -> None`
  - `transaction.atomic()`: lock the project row (as above). If `user` is no
    longer a member (left in another tab), raise `NotProjectMemberError`.
    Count `project.members.filter(is_active=True).exclude(pk=user.pk)`. If 0,
    raise `LastProjectMemberError`. Otherwise `project.members.remove(user)`.
  - Why the lock: two of the last two members leaving at the same moment would
    each count one other member and both leave. With the row lock the second
    transaction counts after the first commits. Under Postgres READ COMMITTED,
    each statement sees data committed before it starts, so the second count
    sees 0.
  - "Others available" means other **active** members. A project left with only
    deactivated accounts would be orphaned (see Q3).

Domain exceptions in **`projects/exceptions.py`**: a base
`ProjectMembershipError(Exception)` with a user-safe `message` (same pattern as
`TestRailRunImportError`), and the subclasses `MemberNotFoundError`,
`AlreadyMemberError`, `MemberInviteThrottledError(window_seconds)`,
`LastProjectMemberError` and `NotProjectMemberError`.

Nothing is added to `projects/services.py`.

### Pages and forms
New form **`ProjectMemberInviteForm`** in `projects/forms.py`: one field,
`email = forms.EmailField(label="Email", max_length=254)`, with an
`input` class widget and `autocomplete="off"`.

New views in **`projects/member_views.py`**. They are thin: the form, the
service call, and errors mapped to the form or to messages.

| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `/projects/<id>/settings/` (existing `projects:settings`) | GET, POST | `@project_membership_required` | `TestRailSettingsForm` (unchanged) + unbound `ProjectMemberInviteForm` | `projects/settings.html`; existing context + `members: list[ProjectMemberRow]`, `can_leave: bool`, `invite_form` | unchanged |
| `/projects/<id>/settings/members/invite/` → `projects:member_invite` | POST | `@project_membership_required`, `@require_POST` | `ProjectMemberInviteForm` | Success: `messages.success("<email> now has access to this project.")`, redirect to `projects:settings` (PRG). Failure: re-render `projects/settings.html` with the bound invite form | invalid email, `MemberNotFoundError` ("No active user with that email."), `AlreadyMemberError`, `MemberInviteThrottledError` → `form.add_error("email", exc.message)`, page re-rendered with status 200 |
| `/projects/<id>/leave/` → `projects:leave` | POST | `@project_membership_required`, `@require_POST` | none (confirm dialog) | Success: `messages.success('You left "<name>".')`, redirect to `projects:list` | `LastProjectMemberError` → `messages.error`, redirect to `projects:settings`. `NotProjectMemberError` cannot happen past the decorator except in a race; map it to a redirect to `projects:list` |

Settings rendering: rename `testrail_views._render_settings_page` to the public
`render_project_settings(request, project, *, testrail_form, invite_form)`. It
calls `list_project_members` and adds `members`, `can_leave`, `invite_form`.
`member_views.member_invite` uses it to re-render with field errors. Moving
`project_settings` out of `testrail_views.py` would also work but is
unnecessary churn.

Template **`templates/projects/settings.html`**: update the context comment. Add
a **Members** card above the TestRail card:
- A `.data-table` with `data-label` cells (Member, Name, a status badge: "You",
  "Inactive"). Emails and names are auto-escaped, never `|safe`.
- An invite form: `partials/form_field.html` with `field=invite_form.email`, a
  "Add member" `btn-primary`, and a hint that the person must already have an
  account and turns on notifications in their own Notifications settings.
- A "Leave project" `btn-ghost` that opens `partials/confirm_dialog.html`
  (`dialog_id="leave-project"`, body "You lose access to this project and stop
  getting its notifications. Another member can add you again.",
  `action_url={% url 'projects:leave' project.id %}`). When `not can_leave`,
  show a disabled button with the hint "You are the only member. Add someone
  before you leave." The service still enforces the rule.
- Add new CSS classes to `static/css/punk-hazard.css` only if the existing
  `.card`, `.data-table` and `.badge-*` do not cover it. No inline styles.

`projects/urls.py`: add the two routes.

### Background work and WebSockets
- No new Celery tasks, consumers or events. There are no controller protocol
  changes (`CLIENT_VERSION` and capabilities are untouched).
- Notifications need no code change (see Context).
- A leaver's open sockets: membership is checked at `connect()`. A socket that
  was open before leaving keeps receiving read-only project events until the
  page is closed or reconnects, which then fails. Every write it could make
  goes through a membership-checked view. We accept this as residual for a
  *voluntary* leave. If removal of others is ever added (Q2), revisit this with
  a per-user channel group to close sockets.

### Settings / infrastructure
None. Throttle limits are module constants. The Redis cache already exists.
No `example.env` change.

### Data flow
- Invite: settings page form → `member_invite` view (decorator resolves the
  project) → `ProjectMemberInviteForm` → `invite_member` (throttle → user
  lookup → lock project row → already-member check → `members.add`) → PRG back
  to settings. The new member sees the project in their list right away. They
  get notifications once their own preference is on.
- Leave: confirm dialog → `leave` view → `leave_project` (lock → count other
  active members → `members.remove`) → redirect to project list.
- Settings GET: decorator (1 query) + TestRail hint + members (1 query).

## Alternatives considered
- **Add a real `username` field to `CustomUser`.** That needs a migration on a
  populated table with a backfill and unique constraint, admin changes, and a
  decision about login. Email is already the unique, human-known identifier,
  so this draft uses email (Q1).
- **Invitation with acceptance (a pending-invite model).** It is safer against
  unwanted additions, but it adds a model, a page and a flow nobody asked for.
  Members are colleagues on an admin-provisioned server, and the request says
  invite adds. Rejected.
- **Generic "no such user or already a member" error** to hide existence. It
  hurts usability, and the membership list is already visible to the inviter.
  Existence of an active account is the only fact revealed, and it is
  throttled. On a closed, admin-provisioned instance that is acceptable.
- **Put the use cases in `projects/services.py`.** Forbidden by CLAUDE.md
  (legacy module).
- **Separate modules per use case** (`project_invite_services.py`,
  `project_leave_services.py`). That is defensible, but the three functions
  share the lock and the "active member" rule. One cohesive membership module
  is simpler.

## Risks and mitigations
- **Username probing**: the throttle (10 per 10 min per inviter) and one
  message for unknown and inactive accounts limit it. Only authenticated
  project members can reach the form.
- **Orphaned project** (concurrent leaves): the project row lock plus the count
  inside the transaction prevent it.
- **Lock contention**: other code also locks the project row briefly (e.g.
  `machine_setup/services/stop_turn.py`). Invite and leave hold it for a few
  short queries only.
- **Any member can do destructive things** (archive, regenerate the connection
  key, clear TestRail settings) for everyone. That is by design ("manage
  everything") and is already true today. Point it out to the user.
- **XSS**: member emails and names are user data. Render them with
  autoescape only.
- **Residual socket access after leaving**: see above.
- **N+1**: the members list is one query with `.only(...)`, and `can_leave` is
  derived in Python. The project list member count is already annotated.

## Implementation steps
1. Domain exceptions (files: `projects/exceptions.py`).
2. Membership services and the row dataclasses
   (files: `projects/project_member_services.py`).
3. Invite form (files: `projects/forms.py`).
4. Views and routes: `member_invite` and `leave` in a new module. Make the
   settings render helper public and add the members context
   (files: `projects/member_views.py`, `projects/testrail_views.py`,
   `projects/urls.py`).
5. Settings template Members card, invite form and leave dialog. Add CSS only
   if needed (files: `templates/projects/settings.html`,
   `static/css/punk-hazard.css`).
6. Docs: `docs/changed/<date>-project-sharing.md`, and a one-line CLAUDE.md
   note if the orchestrator wants it.

Steps 1–3 are small and shared, so do them first. Steps 4 and 5 can then run
in parallel (different files; the interface is the context keys above).

## Verification
- `pre-commit run --all-files` (black, isort, mypy strict) and
  `python manage.py check`.
- Manual, with two or three admin-created users A, B, C (C deactivated):
  - A opens Settings and sees one row "You". Leave is disabled with the hint.
  - A invites `b@…` in different case (`B@…`) → success message, B listed.
    B's project list shows the project, and B can open runs and settings.
  - A invites B again → "already a member" on the field. A invites C
    (inactive) and a made-up email → the same "No active user" message.
  - Submit 11 invites within 10 minutes → the 11th shows the throttle message.
  - B turns on notifications. Trigger a Failed case (or use a Need help
    request) → both A and B (if opted in) get it.
  - B leaves → redirected to the list, project gone, opening its URL gives
    404. A is now alone, and A's Leave is refused (button disabled; a forged
    POST gets the error message).
  - Concurrency: with A and B as the only members, POST leave from both in
    quick succession (two sessions with curl) → exactly one succeeds.
  - Members query: with `django.db.connection.queries` / debug logging,
    confirm the Settings GET adds exactly one query for members.
- Run the `reviewer` and `verifier` agents.

## Open questions
- Q1: The user model has no username; email is the login identifier. Is
  invite-by-email OK, or do you want a separate username field added to
  accounts? (Draft assumption: invite by exact email, case-insensitive.)
- Q2: Should a member be able to remove *other* members? (Draft assumption:
  no, only self-leave as requested.)
- Q3: Should "others available" count only active accounts, so you cannot
  leave a project whose only other members are deactivated? (Draft
  assumption: yes, only active members count.)

## Changelog
- 2026-10-27: created
- 2026-10-28: Q1 email, Q2 no removal, Q3 active only (user answers)
- 2026-10-28: review fixes: exact-email lookup before `iexact`; `render_project_settings` builds default forms itself, so `member_views` no longer builds the TestRail form. Rejected for now: moving the renderer to a neutral `settings_views.py` (churn; revisit when a third Settings card is added).
- 2026-10-28: Done
