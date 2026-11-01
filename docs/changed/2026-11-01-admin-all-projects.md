# Superuser access to every project and an "All projects" page

Date: 2026-11-01
Plan: docs/plan/feat-admin-all-projects.md

## What changed
- `projects/project_access.py`: `has_superuser_project_control` (active + superuser), the one predicate; `ProjectAccess` gains `via_superuser`, `is_member`, `is_admin_access`; `can_write` includes superusers.
- `projects/project_access_services.get_project_access`: one query on `Project` with a membership-role subquery; superusers get access without (or with a read-only) membership. `projects.services.get_project_for_user` (every project WebSocket) delegates to it.
- `projects/context_processors.project_access`: `project_admin_access`; "Admin access" badge (`templates/partials/badge_admin_access.html`) in the project layout and the five wizard steps.
- `projects/project_member_services.py`: `_ensure_writer` accepts superusers; `list_project_members(viewer=, viewer_access=)`, Leave only for real members.
- `projects/views.project_archive` / `project_restore`: optional validated `next`.
- `test_sessions/services/case_outcome._owner_has_access`, `session_queries.list_session_cases`: superuser owners keep live values and case links on non-member projects (predicate mirrored, import rule).
- New `/all-projects/` (`dashboard:all_projects`): `dashboard/all_projects_types.py`, `all_projects_forms.py`, `all_projects_views.py`, `services/all_projects.py`, `templates/dashboard/all_projects.html`; superuser-only nav link. Search name / member email / tag; filters status, Test Runner, runs, my membership; sort newest / name / activity; Open / Archive / Restore. 5 queries per page regardless of size.

## Why
Admins need to see and control every project, not only those they were invited to.

## Behaviour and contract changes
- Reverses the user-management rule "superusers have no implicit project access" (CLAUDE.md Authorization updated). Superusers can now see every Connection key and drive every test machine.
- New URL `/all-projects/` (GET). Archive / Restore accept POST `next`.
- A paused-help message by a non-member superuser is now sent as them (Celery check uses `get_project_access`).
- No migration, setting, task, socket event or controller change. Deploy: restart web/Daphne and Celery workers.

## Notes for future changes
- Superusers are not members: keep notifications, inbox, attention, dashboard, `/failures/`, `/projects/`, runner email matching and the last-writable-member invariant membership-only.
- Use `has_superuser_project_control`, never `is_superuser` alone, for project access.
- No audit log or "Admin" actor label (Q3); actions carry the superuser's own name.
