# Project sharing: members list, invite by email, leave

Date: 2026-10-28
Plan: docs/plan/feat-project-sharing.md

## What changed
- `projects/project_member_services.py` (new): `list_project_members` (one query, `ProjectMemberRow` / `ProjectMembers.can_leave`), `invite_member` (throttled, adds at once), `leave_project`. Invite and leave lock the project row.
- `projects/exceptions.py`: `ProjectMembershipError` and the subclasses `MemberNotFoundError`, `AlreadyMemberError`, `MemberInviteThrottledError`, `LastProjectMemberError`, `NotProjectMemberError`.
- `projects/forms.py`: `ProjectMemberInviteForm` (email).
- `projects/member_views.py` (new): `member_invite`, `project_leave`.
- `projects/testrail_views.py`: the public `render_project_settings` builds any card form it is not given and adds the members context.
- `templates/projects/settings.html`: Members card (table, "You" / "Inactive" badges, invite field, Leave confirm dialog).

## Why
A project can now be shared: every member manages everything and gets its notifications. Both were already true for every member of `Project.members`, but only Django admin could add a second member.

## Behaviour and contract changes
- New POST URLs, both `@project_membership_required`:
  - `projects:member_invite` (`<id>/settings/members/invite/`, field `email`);
  - `projects:leave` (`<id>/leave/`).
- Invite by email, not username: `CustomUser` has no username. The match is exact first, then case-insensitive if unambiguous. Only active accounts can be added, and unknown and inactive emails get the same message. Each inviter gets 10 attempts per 10 minutes (Redis cache), successful invites included.
- Leave is refused unless another active member remains. There is no removal of other members, and there are no roles.
- No migration, setting, Celery task, socket event or notification code change. A new member gets notifications once they turn them on at `/notifications/`.

## Notes for future changes
- A socket opened before leaving keeps receiving read-only events until it reconnects. Membership is checked only at `connect()`. If removing others is added, close those sockets.
- `render_project_settings` lives in `testrail_views.py`. Move it to a `settings_views.py` when a third Settings card arrives.
- Verified with pre-commit (strict mypy), `manage.py check`, a throwaway-DB smoke script (17 checks: invite paths, throttle, leave rules incl. inactive others, 404/405, escaping; one members query per Settings GET), and two reviewer and verifier passes. Not yet checked in a browser, and the concurrent-leave lock was not exercised.
