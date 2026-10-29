# User management: Users page, superuser-only run mode, read-only members

Date: 2026-10-29
Plan: docs/plan/feat-user-management.md

## What changed
- `projects/models.py`: `ProjectRole` (member / read_only), `WRITABLE_ROLES`, `ProjectMembership` through model for `Project.members` (same table and column as the old auto M2M). Migration `projects.0041_project_membership` (hand-written: state-only plus `ADD COLUMN role`). Admin: membership inline replaces `filter_horizontal` (admin.E013).
- `projects/decorators.py`: `project_membership_required` resolves `ProjectAccess` (`project_access.py`, `project_access_services.get_project_access`, one query) and 403s unsafe methods for read-only members; `project_read_access_required` (Leave only); `get_writable_project` for edit / archive / restore / duplicate. `projects/context_processors.project_access` exposes `project_can_write` / `project_role`.
- `projects/project_member_services.py`, `member_views.py`, `forms.py`, `urls.py`: rows carry role; invite can add read-only; new `change_member_role` + `projects:member_role`; leave and demotion keep an active writable member (`LastWritableMemberError`); inviter/actor re-checked (`MemberManagementNotAllowedError`).
- `projects/run_help_mode_policy.py`: `resolve_help_mode`, called under the run lock in `start_test_run` (now takes `user`, `help_mode` optional), resume / case resume, re-run selected, redesign re-run, and `change_run_help_mode` (non-superusers refused). `HelpModeNotAllowedError` mapped to messages in the views.
- Copy-to-project and the Failures bulk action only target writable projects; `list_projects_for_user` annotates `viewer_can_write`.
- Notifications: `recipient_user_ids`, night digest, `record_missed_failure`, `broadcast_project_attention_changed` select writable members only; dashboard attention uses `writable_member_project_ids`. `runner_person_step_services` resolves only writable members.
- `accounts/user_management_services.py` (`list_users`, `create_user`), `accounts/user_views.py`, `accounts/decorators.superuser_required`, `accounts/exceptions.py`, `UserCreateForm`, `templates/accounts/users.html`, "Users" nav link for superusers.
- Templates: every project and machine-setup write control behind `{% if project_can_write %}`; "Read-only access" badge in project breadcrumbs; run-mode controls only for superusers (`_help_mode_select.html` `can_change`); Members card role column, role select, read-only invite checkbox; connection key and runner download hidden for read-only.

## Why
Admins need to add users in the app, keep the expensive or risky run modes (Night, Human in the loop) to superusers, and share projects with people who should only look and not be notified.

## Behaviour and contract changes
- Migration `projects.0041_project_membership`: every existing member becomes `member` (writable); no data copied or lost. Reverse drops the role column only.
- New URLs: `accounts:users` (`/accounts/users/`, GET), `accounts:user_create` (POST), `projects:member_role` (`<id>/settings/members/<user_id>/role/`, POST, field `role`). Invite form gains `read_only`.
- `TestRunStartForm.help_mode` is optional; non-superusers post none and keep the stored mode; a different value is refused, not ignored. The mid-run switch (including re-choosing the current mode to hand a person step back) is superuser-only.
- New context processor `projects.context_processors.project_access`. No new env var, Celery task, socket event or controller protocol change.
- Deploy: `migrate`, restart Daphne/web and every Celery worker.

## Notes for future changes
- Never regenerate 0041 with `makemigrations` (the autodetector would drop the members table). The parallel test-sessions branch may also add `projects/0041_*`; whoever merges second renumbers.
- A new write view must be POST and use `project_membership_required`; a GET view must not change user-visible state.
- Django admin's membership inline can still demote or remove the last writable member (superuser-only, like deactivating a user).
- Superusers only get superuser powers in projects they are members of; a project without a superuser member runs in its stored mode (Attended for new drafts).
- The Failures bulk bar is still offered on read-only projects' rows; the service skips them and the message says so.
- Verified with pre-commit (strict mypy), `manage.py check`, `makemigrations --check`, `sqlmigrate projects 0041` (only `ADD COLUMN`), a throwaway-DB smoke (76 checks: migration round trip, read-only GETs 200 with no POST forms, 15+ POST families 403 with no change, role rules, help-mode refusal, recipients, runner attribution, Users page), and two reviewer/verifier rounds. Not checked in a browser.
