# feat: Chat reply notifications

Status: Done
Created: 2026-10-24
Request: When the Machine setup agent finishes a turn a person started (reply
ready, step/time limit reached, or the turn failed) and that person is not
looking at the conversation, notify them, the way ChatGPT or a messenger app
does. "Not looking" includes an open but hidden tab. Delivery reuses the
existing ntfy notifications. The chat page also shows an unread count in the
tab title while it is hidden.

## Context
- Turns run in Celery (`machine_setup.tasks.run_setup_turn`, execution queue). Every turn
  the worker runs ends in `machine_setup/services/run_turn.py` `_TurnRunner._finish`. Its
  conditional `RUNNING -> end` update (`ended`) is true at most once per turn. A turn whose
  worker was killed is closed later by `live_turn._close_lost_turn`, but only when someone
  acts on the project. So it is not a "reply arrived" moment, and it is out of scope.
- `SetupTurn.requested_by` is the person who sent the message. It is `None` for the
  Night auto-fix turn (`case_auto_fix.py`, `auto_fix=True`). Person turns in an auto-fix or
  help thread are ordinary turns. The final reply is a `SetupMessage(kind=assistant,
  turn=…, parent=None)` row written by `ActivityRecorder` (`record_final_reply=True`).
  Limit and failure endings write a NOTICE row and `TurnOutcome.notice/error_message`
  (`machine_setup/turn_outcome.py`).
- Pages: the main chat `machine_setup:chat` and a thread `machine_setup:thread`. Both use
  `templates/machine_setup/chat.html`. Sockets: `MachineSetupConsumer`
  (`machine_setup/consumers.py`) uses one project-wide group, and `receive()` currently
  ignores every frame. On the main route the conversation can change under the socket
  (reset, first message). The client tracks it in `this.conversationId`.
- `notifications`: `NotificationPreference` (`notify_need_help`, `notify_failed`),
  `NotificationKind` and the delivery/flag maps in `services/notification_message.py`, and
  `send_to_users`, which re-checks the flag in the worker. The `send_notification` task
  runs on `notify`. The pattern to copy is `projects/case_failed_notifications.py`: build
  the message, then `transaction.on_commit(send_notification.delay)`, never raise.
- Redis is already the Django cache (`django-redis~=6.0.0`), the channel layer and the
  broker. Versions in use: Django ~=5.2.11, channels ~=4.3.2.

## Goals
- An ntfy push to the person who sent the turn when the turn ends COMPLETED (answered, step
  limit or time limit) or FAILED, and none of their tabs shows that conversation in the
  foreground.
- A new kind "Chat replies" on the Notifications page, on by default.
- On the chat page, while hidden, the tab title shows `(N) ` for agent replies and turn
  ends that arrived while hidden. It is cleared when the tab becomes visible.

## Non-goals
- No notification for STOPPED turns (the person pressed Stop or Reset), for lost-worker
  closes, or for the Night auto-fix turn (Need help covers those).
- No message to other project members who are watching or who did not send the turn.
- No in-app unread indicator on other pages (that would need a site-wide socket), no
  favicon dot, and no browser Notification API. ntfy already reaches the desktop and the
  phone, and a second channel would duplicate it.
- No per-thread mute and no read receipts.

## Design

### Data model
- `NotificationPreference.notify_chat_reply = BooleanField(default=True)`. Migration
  `notifications.0003_notify_chat_reply` is a single AddField with a default. Existing
  rows get `True`, the change is additive, and it is safe on a populated table.
- No `machine_setup` model change. Presence lives in Redis only.

### Services
- `machine_setup/services/viewer_presence.py` (new). Presence is kept in the Django cache.
  - Key: `machine_setup:viewing:<conversation_id>:<user_id>`. Value: the socket's
    `channel_name`. TTL: `PRESENCE_TTL_SECONDS = 45`, a code constant. The client sends a
    heartbeat every 15 s.
  - `mark_viewing(conversation_id, user_id, channel_name)` sets the key and refreshes the
    TTL. It is async-friendly (`cache.aset`).
  - `clear_viewing(conversation_id, user_id, channel_name)` deletes the key only if this
    channel owns it (get, then delete). That way one tab hiding does not usually wipe
    another visible tab's presence. The race is accepted: a second visible tab writes the
    key again within 15 s.
  - `is_viewing(conversation_id, user_id) -> bool` is sync, for the worker.
  - Why this design: a key with a TTL works across any number of Daphne processes, cleans
    itself up when a process crashes or a socket is lost, needs no channel-layer group
    introspection (Channels has no "who is in this group" API), and is one Redis GET at
    check time. A tab counts as viewing when it is open, its socket is connected and
    `document.visibilityState === 'visible'`. A visible window without focus still counts,
    as in messengers.
- `machine_setup/services/turn_reply_notification.py` (new):
  `notify_turn_reply(turn: SetupTurn) -> None`. It never raises and logs exceptions.
  - It sends nothing when any of these holds: `turn.auto_fix`; `requested_by_id is None`;
    the status is not COMPLETED or FAILED; ntfy is not configured; `is_viewing(...)` is
    true; the conversation is archived; or the user is no longer a project member.
  - Recipient check: a single query on `NotificationPreference` filtered by `user_id`,
    `enabled`, `notify_chat_reply` and `user__projects__id=project_id`. Add
    `notification_recipient_ids(project_id, kind, user_ids)` beside `recipient_user_ids`
    in `notifications/services/recipients.py` rather than duplicating the query.
  (Implemented as an `among=` keyword on `recipient_user_ids`.)
  - Message: `NotificationKind.CHAT_REPLY`, priority 3, tag `speech_balloon`.
    - Title: `"Reply · <project name>"`. A limit ending uses `"Paused · …"` and FAILED uses
      `"Turn failed · …"`.
    - Body: the turn's last top-level assistant row text. For a limit ending it is the
      notice text, and for FAILED it is `turn.error_message`, which is user-safe. Cap the
      preview at 140 characters (a constant). `NotificationMessage.create` collapses and
      truncates.
    - Path: `machine_setup:chat` for kind `chat`, otherwise `machine_setup:thread`.
  - Enqueue with `transaction.on_commit(send_notification.delay(...))`, as in
    `case_failed_notifications`. Only the user id goes into the task arguments, never the
    topic.
- `notifications/services/notification_message.py`: add `CHAT_REPLY` to the kind enum and
  to the `_DELIVERY` and `_PREFERENCE_FLAG` maps.
- `notifications/services/preferences.py`, `preference_view.py`, `forms.py`, `views.py`:
  add `chat_reply` to `update_kinds`, `PreferenceView` and `NotificationKindsForm`.

### Pages and forms
| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `notifications:kinds` (existing) | POST | login | `NotificationKindsForm` + `chat_reply` | `notifications/settings.html`: new checkbox "Chat replies", with the hint "The setup agent answered while you were away." | unchanged |
| `machine_setup:chat` / `:thread` (existing) | GET | `@project_membership_required` | none | `chat.html`: presence and title-badge JS only, no new context | none |

### Background work and WebSockets
- Client to server, new frame on the existing setup sockets:
  `{"type": "presence", "visible": bool, "conversation_id": int}`.
  - The page sends it on socket open, on `visibilitychange`, and every 15 s while visible.
  - It does not send when `conversationId` is null (a main chat with no conversation yet).
- `MachineSetupConsumer.receive`:
  - parses the frame and ignores bad frames and frames over 256 bytes;
  - validates that `conversation_id` is a non-archived thread of `self._project` through
    `find_thread`, caching the validated ids on the consumer so heartbeats cost no query;
  - calls `mark_viewing` or `clear_viewing`;
  - keeps the last conversation id it marked, so a change on the main route clears the old
    key;
  - `disconnect` clears the key.
  - All other input still goes through forms. The consumer stays thin.
- Hook: `_TurnRunner._finish`, inside the existing `if ended:` block, calls
  `notify_turn_reply(self.turn)` after `settle_case_auto_fix`. Both never raise. No new
  Celery task, and `send_notification` is reused on `notify`.
- Title badge (`chat.html` only): a counter is incremented in `apply()` when the document
  is hidden and either an assistant row of the shown conversation arrives with
  `parent_id == null`, or a turn arrives ending in completed or failed. It sets
  `document.title = '(N) ' + baseTitle`. It resets on `visibilitychange` to visible. No
  server state.

### Settings / infrastructure
None. The constants live in code, and Redis is already the cache.

### Data flow
1. The person sends a message (form POST), the turn is queued, and the person switches tab
   or leaves the page. The tab sends `visible:false` (or the socket closes), and the key is
   deleted or expires within 45 s.
2. The worker finishes the turn: `_finish` makes the conditional update, sends the final
   broadcast, then `notify_turn_reply` checks the turn, `is_viewing` (1 GET) and the
   recipient (1 query), loads the last reply (1 query), and runs `on_commit` then
   `send_notification.delay([user_id], "chat_reply", …)`.
3. The `notify` worker publishes to the user's ntfy topic. Clicking it opens the thread
   URL. If the tab was merely hidden, it also shows `(1) Machine setup …`.

## Alternatives considered
- Channel-layer group membership as presence: Channels exposes no membership query, and
  groups expire only after `group_expiry` (24 h), so this does not work.
- Counting sockets with Redis INCR/DECR: drifts when a Daphne process dies. TTL keys heal
  themselves.
- Browser Notification API, Web Push or a site-wide unread socket: more surface (Web Push
  was just removed). ntfy already does push.
- A DB column for "last seen": a write per heartbeat. Redis is the right store for
  short-lived state.

## Risks and mitigations
- **Reply text leaves the server.** The preview goes through the ntfy server (ntfy.sh by
  default). An agent reply can quote command output, paths or, rarely, credentials it saw.
  Mitigations: cap the preview at 140 characters, never include tool rows, and leave it as
  an open question (Q1). The Failed kind already sends verdict text the same way.
- **Spurious or missed notifications at edges.** A duplicate is possible within 15 s when
  two visible tabs show the same thread and one hides. A missed one is possible when the
  socket died silently less than 45 s before the end. Both are acceptable.
- **Frame abuse.** A member could spam presence frames. Each frame costs one cache write
  and a size cap applies. There is no authorization leak because conversation ids are
  validated against the authorized project.
- **ntfy.sh quota** (about 250 messages a day per IP): at most one push per person turn,
  so it is negligible.
- **Deploy order.** Migrate before restarting the workers. Otherwise `send_to_users`
  filters on a missing column; it fails, but only for this kind.

## Implementation steps
1. Preference field, migration, kind and maps, recipients helper, preferences/view/form,
   settings checkbox. (files: `notifications/models.py`,
   `notifications/migrations/0003_notify_chat_reply.py`,
   `notifications/services/{notification_message,recipients,preferences,preference_view}.py`,
   `notifications/forms.py`, `notifications/views.py`, `templates/notifications/settings.html`)
2. Presence service. (files: `machine_setup/services/viewer_presence.py`)
3. Consumer presence frames. (files: `machine_setup/consumers.py`) Depends on 2.
4. Notify service and hook. (files: `machine_setup/services/turn_reply_notification.py`,
   `machine_setup/services/run_turn.py`) Depends on 1 and 2.
5. Client presence heartbeat and title badge. (files: `templates/machine_setup/chat.html`)
   Depends on 3's frame contract.

Steps 2, 3, 4 and 5 touch different files and can go to parallel sub-agents once 1 and
2 are in.

## Verification
- `pre-commit run --all-files`, `python manage.py check`,
  `python manage.py makemigrations --check`.
- Manual checks (ntfy app subscribed, Daphne and the execution and notify workers
  running):
  1. Send a message, stay on the page with the tab visible: no push. The `notify` worker
     log shows nothing for `chat_reply`.
  2. Send, switch to another tab: a push "Reply · <project>" arrives with a preview of 140
     characters or fewer. The chat tab title reads `(1) …` and clears when you return.
     Clicking the push opens the chat.
  3. Send, close the tab: push received. Same in a help thread: the link opens that
     thread.
  4. Turn off "Chat replies" on `/notifications/`: no push.
  5. Start a turn that fails (stop the Test Runner mid-turn): "Turn failed" push with the
     user-safe error.
  6. Press Stop: no push. A Night auto-fix turn: no chat-reply push (Need help applies as
     before).
  7. Two Daphne processes (or one restarted mid-turn): presence still respected after the
     socket reconnects (within 15 s).
  8. `redis-cli --scan --pattern '*machine_setup:viewing*'`: keys appear while visible and
     disappear on hide, close, or after 45 s.
- Run the `reviewer` and `verifier` agents.

## Open questions
- Q1 (answered): include a preview of the reply, capped at 140 characters.
- Q2 (answered): "Chat replies" is on by default, existing rows included.

## Changelog
- 2026-10-24: created
- 2026-10-24: Q1/Q2 answered by the user (preview of 140 chars; on by default)
- 2026-10-24: implemented. Badge counts turn ends (completed/failed) only, not
  assistant rows: on arrival a reply cannot be told from narration.
- 2026-10-24: review pass 1 fixes: shared `notifications.services.enqueue_notification`
  (the two older `_enqueue` copies in `projects/case_*_notifications.py` are left for a
  separate refactor); frame parsing in `machine_setup/presence_frame.py` and the
  previous/new switch in `viewer_presence.update_viewing`; unknown ids dropped once the
  per-socket cache is full (no clear); char-length check before encode; `pageshow`
  restarts the heartbeat after a back/forward-cache restore; empty limit notice falls
  back to a fixed body. Rejected: none. Accepted: failed-turn error text goes to ntfy
  (already shown in the chat; previews accepted by the user).
- 2026-10-24: review pass 2 clean. Accepted: no rate limit on presence frames (one
  Redis SET each, 256-byte cap, own key only); a socket that has checked 32 ids drops
  new ones until it reconnects.
