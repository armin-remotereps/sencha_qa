# Chat reply notifications

Date: 2026-10-24
Plan: docs/plan/feat-chat-reply-notifications.md

## What changed
- `notifications`:
  - new kind `CHAT_REPLY` (priority 3, speech balloon) and `NotificationPreference.notify_chat_reply`, on by default;
  - a "Chat replies" checkbox on `/notifications/`;
  - `recipient_user_ids(..., among=)` narrows the recipients to given users;
  - new `services/enqueue_notification.py`.
- `machine_setup/services/viewer_presence.py` (new): `mark_viewing`, `clear_viewing`, `update_viewing` and `is_viewing`. Presence is a Redis cache key per conversation and user, holding the socket's channel name, with a 45 s TTL.
- `machine_setup/presence_frame.py` (new): parses the client's presence frame.
- `MachineSetupConsumer.receive`:
  - handles presence frames;
  - checks each conversation id with `find_thread`, cached per socket and capped at 32;
  - `disconnect` clears the socket's key.
- `machine_setup/services/turn_reply_notification.py` (new): `notify_turn_reply(turn, notice=)` is called from `_TurnRunner._finish` once a turn has ended.
  - It sends a push to the person who sent the turn when the turn ends COMPLETED or FAILED and that person is not viewing the conversation.
  - Titles: "Reply ·", "Paused ·" (limit reached) or "Turn failed ·", followed by the project name.
  - Body: a 140-character preview.
  - Link: the main chat or the thread.
- `templates/machine_setup/chat.html`:
  - sends a presence heartbeat on open, on `visibilitychange`, every 15 s while visible, on `pagehide` and on a back/forward-cache `pageshow`;
  - shows a `(N) ` tab-title badge for turns that end while the tab is hidden.

## Why
Agent turns take minutes and people leave the page. Like a messenger, the person should be told when the agent answers, but only if they are not looking.

## Behaviour and contract changes
- New WebSocket client frame on the setup sockets: `{"type":"presence","visible":bool,"conversation_id":int}`. The size cap is 256 bytes; bad frames are ignored.
- Migration `notifications.0003_notify_chat_reply`: one AddField with default True. Existing users get chat reply pushes.
- No new settings or Celery task (it reuses `send_notification` on `notify`).
- Deploy: run `migrate`, then restart Daphne/web, the execution worker and the notify worker.

## Notes for future changes
- No push for Stop, Reset, the Night auto-fix turn, an archived thread, or a turn closed later as lost by `live_turn`.
- The reply preview goes through the ntfy server (ntfy.sh by default). The user accepted this.
- The badge counts turn ends, not reply rows: when an assistant row arrives, the page cannot yet tell a reply from narration.
- Accepted edges:
  - two visible tabs on one thread can lose presence for up to 15 s when one tab hides;
  - presence frames are not rate limited.
- Follow-up: `projects/case_help_notifications.py` and `case_failed_notifications.py` still have their own `_enqueue`; switch them to `enqueue_notification`.
- Verified with pre-commit (strict mypy), `manage.py check`, `makemigrations --check`, and two reviewer and verifier passes. Not yet exercised with a live socket, Redis presence keys or a real ntfy push (plan Verification, manual steps 1–8).
- pre-commit's black wants to reformat `projects/migrations/0022_blocked_reason_runner_outdated.py`. This predates this change and was left alone.
