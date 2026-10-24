# refactor: Replace Web Push with ntfy, and add Failed case notifications

Status: In progress
Created: 2026-10-24
Request: Swap the Web Push notifications from `feat-human-in-the-loop` for
[ntfy](https://ntfy.sh). People install the ntfy phone or desktop app and
subscribe to a topic. The server publishes to that topic with one plain HTTPS
POST, so there are no VAPID keys, no service worker and no browser
subscription. Remove the Web Push code, its table, its views, its JS, the
`pywebpush` dependency and the `WEBPUSH_*` settings.
`notify_case_needs_help(help_request_id, *, after_auto_fix=False)` stays the
single Need help entry point. Added requirement: a case the agent judges
**Failed** also sends a notification, at a lower priority, to the same
recipients. It uses a second entry point next to the Need help one. It is sent
on commit, never raises, and is never sent for a stale generation.

## Context

- `notifications/` (added in `06f12c2`, see
  `docs/changed/2026-10-24-human-in-the-loop.md`):
  - `models.PushSubscription`, with migration `0001_initial` (user FK, endpoint, keys, user agent, timestamps);
  - `services/`:
    - `subscriptions.py`: save, delete and list devices, UA labels;
    - `send_push.py`: pywebpush, a `ThreadPoolExecutor` of 8, 404/410 deletes the row;
    - `endpoint_policy.py`: SSRF allowlist;
    - `push_config.py`: VAPID-configured check;
    - `push_message.py`: `PushMessage` dataclass;
    - `test_push.py`: throttled through an atomic `cache.add`, 30 s.
  - `tasks.send_push(user_ids, title, body, url, tag)` on the `notify` queue, with `max_retries=0`;
  - `forms.py`, `views.py` (`notification_settings`, `subscribe`, `unsubscribe`, `test_notification`, `service_worker`, `web_app_manifest`) and `urls.py` (included at the site root);
  - `admin.py`: read-only `PushSubscriptionAdmin`;
  - `exceptions.py`.
- Templates: `templates/notifications/settings.html`, `sw.js`, `manifest.webmanifest`. `templates/base.html` lines 10-14 hold the manifest link, `theme-color`, `apple-touch-icon` and `apple-mobile-web-app-title`.
- `static/js/punk-hazard.js` holds:
  - the `pushDevice` Alpine component;
  - `punkHazard.pushSupported`, `isIos`, `isStandalone`, `registerServiceWorker` and `base64UrlToBytes`;
  - the global service-worker registration on `load`.
- `static/css/punk-hazard.css` holds `.push-device-list` and `.push-hint`.
- Icons:
  - `static/branding/punk-hazard-icon-192.png` and `-512.png` are used only by the manifest and `sw.js`;
  - `punk-hazard-apple-touch-icon.png` is used only by `base.html`;
  - `punk-hazard-mark.png` and `-logo.png` are used elsewhere and stay.
- `projects/case_help_notifications.py`:
  - `notify_case_needs_help` loads the help request in one query and picks members with `project.members.filter(push_subscriptions__isnull=False)`;
  - it enqueues `send_push` on commit and never raises;
  - it is called from `projects/services.py::_pause_run_for_help` (inside `_after_commit`) and `projects/case_help_state.py` (with `after_auto_fix=True`).
- The Failed verdict path:
  - `projects/services.py::execute_test_run_test_case` calls `_finalize_pivot(pivot, result)` only when `not _is_stale_execution(run_id, generation)`;
  - `_finalize_pivot` sets `FAILED` when the stop reason is neither `CANCELLED` nor `TASK_COMPLETE`, stores `_extract_agent_summary(result)` in `pivot.result` and saves;
  - it runs outside any `atomic` block and holds no run lock, so a newer generation can start between the stale check and the save.
  - `projects/case_help_services.py:251` ("Mark failed") is a person's action, not the agent's verdict.
- Settings:
  - `auto_tester/settings.py` lines 377-396 hold `WEBPUSH_*`, and `example.env` lines 89-100 mirror them;
  - `CELERY_TASK_ROUTES` routes `notifications.tasks.send_push` to `notify`;
  - `docker-compose.yml` runs `notify-celery` with `--concurrency=2`.
- `requirements.txt`:
  - `httpx~=0.28.1` is already a direct dependency, used by `projects/testrail_client.py`, `agents/services/llm_client.py` and `tools_search.py`;
  - `requests` is not a direct dependency (it came in only through pywebpush);
  - `pywebpush~=2.5.0` comes with `stubs/pywebpush.pyi`;
  - there is no QR library.
- There is no site or base URL setting. TestRail links use `request.build_absolute_uri("/")` in views (`projects/testrail_views.py`), which a Celery worker cannot call. `CSRF_TRUSTED_ORIGINS` defaults to `https://sench.remotereps.com`.
- `CLAUDE.md`, "Need help" section: the `notifications` bullet describes Web Push.

### ntfy facts this plan relies on (docs.ntfy.sh, checked 2026-10-24)

- **JSON publish:** `POST <server>/` (the root, not the topic URL) with a JSON body.
  - Fields: `topic` (required), `message`, `title`, `tags` (string array), `priority` (int 1-5), `click` (URL), `markdown`, `icon`, `actions` (up to 3), and others.
  - UTF-8 is safe in the body. Header publishing needs RFC 2047 encoding for non-ASCII, and project names and case titles can be non-ASCII.
- **Priorities:** 5 max/urgent, 4 high, 3 default, 2 low, 1 min.
- **Tags:** a tag that matches an emoji short code renders as that emoji (`sos`, `x`, `white_check_mark`).
- **Auth:** `Authorization: Bearer tk_...` (an access token), or Basic. Use HTTPS, because the credentials are only base64.
- **Topic names:** `[-_A-Za-z0-9]`, at most 64 characters. On ntfy.sh, "all topics are public": anyone who knows the name can read and write, so the topic works as a password.
- **Limits:**
  - default request bucket: 60 per visitor (IP), refilled at 1 per 5 s;
  - messages up to 4,096 bytes (longer ones become attachments);
  - an oversized title or tags is rejected with HTTP 400;
  - **ntfy.sh free tier: 250 messages per day per visitor**, with temporary IP bans for repeated abuse.
- **Self-hosting:**
  - `auth-default-access: deny-all` plus `ntfy access` / `ntfy token add` gives private topics and a publish token;
  - iOS instant delivery from a self-hosted server needs `upstream-base-url: https://ntfy.sh`;
  - set `behind-proxy` when the server runs behind nginx.
- **Subscribe links:**
  - the Android app accepts `ntfy://<host>/<topic>?display=<name>` (add `secure=false` for http);
  - iOS has no documented deep link, so iOS users copy the server and topic into the app;
  - `<server>/<topic>` opens the ntfy web app, where a desktop browser can subscribe.
- **Python client:** `httpx` 0.28 (already pinned). `httpx.Client` is thread-safe, takes a `timeout=` and bounds its connection pool with `httpx.Limits`. A dedicated ntfy library would add nothing over a single POST.

## Goals

- Need help and agent-Failed cases reach each opted-in project member through their own ntfy topic.
- Settings are env-driven:
  - `NTFY_SERVER_URL` (default `https://ntfy.sh`; empty disables notifications);
  - `NTFY_ACCESS_TOKEN` (optional);
  - `NTFY_TOPIC_PREFIX` (optional);
  - `NTFY_REQUEST_TIMEOUT`;
  - `SITE_URL`, for absolute click URLs.
- Per-user Notifications page:
  - an on/off switch;
  - the topic, shown only to its owner, with a copy button;
  - an "Open in ntfy" link, a web link and a Regenerate button;
  - two kind checkboxes (Need help, Failed);
  - the throttled "Send a test notification" button.
- Delivery runs on the `notify` queue with a timeout and bounded concurrency. There are no retries, and a 429 stops the batch.
- The topic and the token are never logged and never put in Celery task arguments.
- All Web Push code, data, assets and the dependency are removed.

## Non-goals

- Blocked, Passed or Cancelled case notifications (Q2 default), and a run-finished summary (Q3 default).
- Email, Slack and ntfy action buttons ("Continue" from the notification).
- Per-project topics (Q1 default is per user).
- A QR code (Q4 default: no new dependency).
- Running or configuring an ntfy server. Deployment docs only recommend one.
- Changing how TestRail links build absolute URLs. `SITE_URL` could serve them later, in a separate plan.

## Design

### Data model

`notifications` migration `0002_ntfy`. The table drop is irreversible: the subscriptions are worthless without pywebpush.

- `DeleteModel PushSubscription`, which drops `notifications_pushsubscription`.
- `CreateModel NotificationPreference`, one row per user, created when the user first turns notifications on:
  - `user`: `OneToOneField(AUTH_USER_MODEL, CASCADE, related_name="notification_preference")`.
  - `topic`: `CharField(max_length=64, unique=True)`. It is the prefix plus `secrets.token_urlsafe(24)` (32 characters, about 192 bits, URL-safe alphabet equal to ntfy's). It is a secret: never rendered except to its owner, never logged, and not shown in the admin.
  - `enabled`: `BooleanField(default=True)`. The row exists only once the user turned notifications on. Off keeps the row, so turning back on keeps the same subscription.
  - `notify_need_help`: `BooleanField(default=True)`.
  - `notify_failed`: `BooleanField(default=True)` (Q5).
  - `created_at` and `updated_at`.
  - Index: the unique `topic` index, plus the `user` one-to-one. Recipient queries filter by `user_id IN (...)` with `enabled` and the kind flag, which is small per project. No extra index.

I put it in a new model rather than on `CustomUser`, so the accounts model stays generic and the secret has one owner table. There is no `last_sent_at`: ntfy acceptance is not delivery, and the ntfy app shows its own history.

### Services (`notifications/services/`)

One use case per module.

- **`ntfy_config.py`**
  - `ntfy_configured() -> bool`: `NTFY_SERVER_URL` is non-empty and http(s), and `NTFY_TOPIC_PREFIX` matches `[-_A-Za-z0-9]{0,16}`. An invalid value logs a warning without the value and counts as not configured.
  - `ensure_ntfy_configured()` raises `NotificationsNotConfiguredError`.
  - `ntfy_server_url() -> str`, without the trailing slash.
  - `ntfy_subscribe_links(topic) -> NtfySubscribeLinks`: a dataclass with `app_url` (`ntfy://host/topic?display=<brand>`, plus `&secure=false` for http), `web_url` (`<server>/<topic>`) and `server_url`. This is a pure URL build; it lives here because it depends on the configured server.
- **`site_url.py`**: `absolute_url(path: str) -> str | None`. It returns `SITE_URL` (stripped) plus a same-origin path, or `None` when `SITE_URL` is empty, in which case the notification has no `click`.
- **`notification_message.py`**:
  - `NotificationKind` enum: `NEED_HELP`, `FAILED`, `TEST`.
  - Frozen `NotificationMessage(kind, title, body, path)`, with a builder that whitespace-collapses and truncates: title 120 characters, body 300 characters (well under the 4,096-byte limit and lock-screen useful).
  - A kind-to-delivery mapping. This mapping is the domain decision, kept in one place:

    | Kind | Priority | Tags |
    | --- | --- | --- |
    | `NEED_HELP` | 4 (high) | `["sos"]` |
    | `FAILED` | 3 (default) | `["x"]` |
    | `TEST` | 3 | `["white_check_mark"]` |

  - The kind also selects the preference flag (`notify_need_help`, `notify_failed`; `TEST` ignores flags).
- **`preferences.py`**: the owner-only operations on the user's own row.
  - `get_preference_view(user) -> PreferenceView | None`. It returns a dataclass with `enabled`, `topic`, `notify_need_help`, `notify_failed` and `links`, or `None` when no row exists. It is only called for the request's own user.
  - `enable(user)`: `get_or_create` with a fresh topic, `enabled=True`.
  - `disable(user)`.
  - `regenerate_topic(user)`: a new topic, saved atomically. A unique collision is astronomically unlikely; retry once, then raise.
  - `update_kinds(user, *, need_help: bool, failed: bool)`.
  - `recipient_user_ids(project_id, kind) -> list[int]`: one query through `project.members` joined to preferences where `enabled` is true and the kind flag is true.
- **`send_notification.py`**: `send_to_users(*, user_ids, message) -> int`, the number of topics accepted.
  - It returns `0` when not configured.
  - One query loads `(user_id, topic)` for `user_ids` where `enabled` and the kind flag still hold. The check is repeated at send time, so a user who opted out after enqueue gets nothing.
  - It builds the click URL with `absolute_url(message.path)`.
  - It opens one `httpx.Client` with:
    - `base_url=NTFY_SERVER_URL`;
    - `timeout=NTFY_REQUEST_TIMEOUT`;
    - `limits=httpx.Limits(max_connections=4)`;
    - the `Authorization: Bearer` header only when `NTFY_ACCESS_TOKEN` is set;
    - `follow_redirects=False`.
  - It POSTs JSON to `/` from a `ThreadPoolExecutor(max_workers=min(4, n))`. The client is shared and closed by a `with` block.
  - Outcomes:
    - 2xx is sent.
    - 429 sets a shared "rate limited" flag, and the remaining sends are skipped (no retry storm). One warning is logged with counts.
    - Other 4xx/5xx and `httpx.HTTPError` log the user id, the status or exception type name and nothing else. The URL is the server root, and the topic is only in the body, which is never logged.
  - It never raises for one user's failure.
  - Topics are not deleted on failure: ntfy has no "gone" signal.
- **`test_notification.py`** (replaces `test_push.py`): `send_test_notification(user) -> None`.
  - It runs `ensure_ntfy_configured()`, then requires an enabled preference (`NotificationsOffError`).
  - The `cache.add` throttle is kept, with a renamed key and the same 30 s window.
  - It enqueues the task on commit with kind `TEST`, path = the Notifications page.

### Exceptions (`notifications/exceptions.py`)

- `NotificationError` stays as the base.
- `PushNotConfiguredError` becomes `NotificationsNotConfiguredError`.
- `NoPushDevicesError` becomes `NotificationsOffError` ("Turn notifications on first.").
- `TestPushThrottledError` becomes `TestNotificationThrottledError`.
- `PushEndpointNotAllowedError` is deleted, because no user-supplied URL is ever contacted.

### Task (`notifications/tasks.py`)

- `send_notification(user_ids: list[int], kind: str, title: str, body: str, path: str) -> None`, registered as `notifications.tasks.send_notification` on the `notify` queue.
  - It keeps `max_retries=0`, `soft_time_limit=60` and `time_limit=90`. Each request is bounded by `NTFY_REQUEST_TIMEOUT` (default 10 s), with four in parallel.
  - It never takes a topic or token. Celery arguments land in redis and are shown in Flower and, with extended results, in `django_celery_results`.
- `CELERY_TASK_ROUTES` replaces the `send_push` entry.

### Project entry points

Each module is a leaf: it imports only `notifications.services` and `notifications.tasks`, never `projects.services`.

- **`projects/case_help_notifications.py`**: the signature and the never-raise contract are unchanged.
  - Replace the `push_configured` and `PushMessage` imports, and the `push_subscriptions__isnull` recipient query, with `ntfy_configured()`, `NotificationMessage(kind=NEED_HELP, ...)` and `recipient_user_ids(project.pk, NEED_HELP)`.
  - The title is "Needs help · <project>" or "Auto-fix could not fix it · <project>", and the body is "<case title>: <ask or reason>", as today.
  - The `tag` field goes: ntfy tags are emoji labels, not replace keys.
- **New `projects/case_failed_notifications.py`**: `notify_case_failed(pivot_id: int, *, generation: int) -> None`.
  - One query loads the pivot with `select_related("test_case", "test_run__project")` and `only(...)` the title, result, project id and name. It is filtered by `status=FAILED` **and** `test_run__execution_generation=generation`. If a newer Start, Resume or Re-run took over, or the case is no longer Failed, it does nothing. This closes the gap between `_is_stale_execution` and the unlocked `_finalize_pivot` save.
  - The title is "Failed · <project>". The body is "<case title>: <verdict summary>", truncated, from `pivot.result` (`_extract_agent_summary`).
  - The path is `projects:test_run_case_detail`.
  - The recipients come from `recipient_user_ids(project_id, FAILED)`. It enqueues on commit, wraps everything in `try`/`except Exception` with `logger.exception`, and never raises.
- **Call site** in `projects/services.py::execute_test_run_test_case`: right after `_finalize_pivot(pivot, result)`, add `if pivot.status == TestRunTestCaseStatus.FAILED: notify_case_failed(pivot.pk, generation=generation)`. `_finalize_pivot` itself is unchanged.
  - This covers only the agent verdict path. Not covered:
    - "Mark failed" in `case_help_services` (a person did it, so they already know);
    - `_settle_pivot_after_error` (that is Blocked);
    - cancellations.

### Pages and forms

All routes are under `/notifications/`. `notifications.urls` stays included at the root, minus the root-level `sw.js` and manifest routes.

| URL | Method | Permission | Form | Template / context | Errors |
| --- | --- | --- | --- | --- | --- |
| `notifications:settings` `/notifications/` | GET | `@login_required` | none | `notifications/settings.html`. Context: `active_nav="notifications"`, `ntfy_configured: bool`, `preference: PreferenceView \| None` (own user only), `kinds_form: NotificationKindsForm` (initial from the preference). | none |
| `notifications:enable` `/notifications/enable/` | POST | `@login_required` | none | redirect to settings, with a success message | not configured: error message |
| `notifications:disable` `/notifications/disable/` | POST | `@login_required` | none | redirect | none |
| `notifications:regenerate` `/notifications/regenerate/` | POST | `@login_required` | none | redirect, with "Subscribe to the new topic in ntfy; the old one gets nothing." | no preference or not configured: error message |
| `notifications:kinds` `/notifications/kinds/` | POST | `@login_required` | `NotificationKindsForm` (`need_help`, `failed`, both `BooleanField(required=False)`) | redirect | invalid form or no preference: error message |
| `notifications:test` `/notifications/test/` | POST | `@login_required`, throttled 30 s per user | none | redirect | `NotificationError.message` |

- The views are thin: call the service, and map `NotificationError` to `messages.error`.
- The forms in `notifications/forms.py` replace `PushSubscriptionForm` and `PushUnsubscribeForm`.
- **Template** `templates/notifications/settings.html`, rewritten with the same layout and existing classes:
  - **Not configured**: an info alert naming `NTFY_SERVER_URL`.
  - **Off or no row**: a short explainer ("Install ntfy on your phone or computer, then turn notifications on") with store and web links to ntfy.sh, and the "Turn on" button.
  - **On**:
    - "Your topic": server URL and topic, each with the existing `clipboard` Alpine component;
    - an "Open in ntfy app" (`app_url`) and "Open in ntfy web" (`web_url`) link pair;
    - an iOS note: add the server and topic by hand in the app;
    - the kinds form with two checkboxes;
    - the Test, Regenerate (inside the existing `dialog` confirm pattern) and Turn off buttons;
    - a warning that the topic works like a password.
  - All values are autoescaped, with no `|safe`. Links use `href` with server-built URLs only; the topic alphabet is URL-safe.
  - The context contract comment is updated.
- **JS**: delete `pushDevice`, the `punkHazard.pushSupported`, `isIos`, `isStandalone`, `registerServiceWorker` and `base64UrlToBytes` helpers, and the global `serviceWorker.register` block. No new JS: the page uses plain forms plus the existing `clipboard` and `dialog` components.
- **CSS**: delete `.push-device-list` and `.push-hint`. If the topic row needs a monospace, wrapping block, add one `.ntfy-topic` class to `punk-hazard.css`.

### Background work and WebSockets

- `notifications.tasks.send_notification` runs on the `notify` queue. The `notify-celery` worker (concurrency 2) and the beat sweep are unchanged.
- There are no WebSocket or controller changes. The controller protocol is untouched, so `CLIENT_VERSION` and the capabilities are unaffected.

### Settings and infrastructure

- **`auto_tester/settings.py`**: remove all `WEBPUSH_*` and add:
  - `NTFY_SERVER_URL: str`, default `"https://ntfy.sh"`. Empty disables notifications.
  - `NTFY_ACCESS_TOKEN: str`, default `""`. A secret, sent only as a Bearer header.
  - `NTFY_TOPIC_PREFIX: str`, default `""`, for example `punkhazard-`. It is validated by `ntfy_config` and at most 16 characters, so that prefix plus 32 is at most 64.
  - `NTFY_REQUEST_TIMEOUT: int`, default 10.
  - `SITE_URL: str`, default `""`, for example `https://sench.remotereps.com`. It is used for click links; empty means no click link.
- **`example.env`**: replace the Web Push block with these settings. The comments cover:
  - topics on ntfy.sh are public, so the random topic is the only protection;
  - the recommended self-hosted setup: `auth-default-access: deny-all`, a publisher token in `NTFY_ACCESS_TOKEN`, `upstream-base-url: https://ntfy.sh` for iOS, `behind-proxy`;
  - the ntfy.sh free-tier limit of 250 messages per day per server IP.
- **`requirements.txt`**: remove `pywebpush~=2.5.0`. Nothing is added; `httpx` is already present. Delete `stubs/pywebpush.pyi`.
- **`templates/base.html`**: remove the manifest link, `apple-touch-icon`, `apple-mobile-web-app-title` and the comment. Keep `theme-color`, which is harmless browser chrome tinting and not push-specific.
- **Delete**:
  - `templates/notifications/sw.js` and `manifest.webmanifest`;
  - `static/branding/punk-hazard-icon-192.png`, `-512.png` and `-apple-touch-icon.png`, which are used only by the PWA and push.
  - Browsers that registered `/sw.js` keep an inert worker: it has no `fetch` handler and no subscriptions remain. The 404 on its update check is harmless.
- **`auto_tester/urls.py`**: update the comment (no root-level files).
- **`CLAUDE.md`**: rewrite the `notifications` bullet, and change "(Web Push)" to "(ntfy)" in the Attended line. Mention the Failed notification.
- **`notifications/admin.py`**: a read-only `NotificationPreferenceAdmin` (user, enabled, kinds, timestamps). It never shows `topic`, and has no add, change or delete.

### Data flow

**Need help:**
1. `_pause_run_for_help` or `case_help_state` calls `notify_case_needs_help(id)`.
2. One query loads the request, and one query loads the recipient ids.
3. On commit, `send_notification.delay(user_ids, "need_help", title, body, path)`.
4. The `notify` worker runs `send_to_users`: one query for (user_id, topic), still enabled and still opted in.
5. It sends up to 4 parallel `POST <NTFY_SERVER_URL>/` with JSON `{topic, title, message, priority: 4, tags: ["sos"], click}`.
6. ntfy fans out to the user's subscribed apps. A click opens the case page in a browser, which goes through the normal login and membership checks.

**Failed:**
1. `execute_test_run_test_case` runs, the stale check passes, and `_finalize_pivot` sets FAILED.
2. `notify_case_failed(pivot.pk, generation=g)` runs one query (FAILED and generation g), then one query for recipient ids.
3. On commit, the task is enqueued, and the flow continues as above with priority 3 and `["x"]`.

**Settings page:** GET reads the own preference. POSTs go through a service and redirect (PRG).

## Alternatives considered

- **Per-project topic, or one topic for everyone.** It means one POST per event instead of one per member, which is easier on ntfy.sh limits. But every member sees the same secret, removing a member means regenerating the topic for everyone, and per-person kind choices are impossible. Per-user is chosen; see Q1.
- **Header publish to `<server>/<topic>`.** It is simpler and the docs' main example. It is rejected because:
  - non-ASCII titles need RFC 2047 encoding;
  - the topic sits in the URL, where httpx's INFO request log, proxies and nginx access logs would record it.
  
  JSON to the root keeps the topic in the body only.
- **A dedicated ntfy Python library.** It adds a dependency, and possibly missing types, for a single POST. Rejected in favour of `httpx`, which is already pinned and typed.
- **Passing topics in task arguments.** It saves one query in the worker, but leaks secrets to redis, Flower and the result backend. Rejected.
- **Recording delivery (`last_sent_at`) or auto-disabling on errors.** ntfy has no "gone" signal, and acceptance is not delivery. Left out for simplicity.
- **Keeping the PWA manifest.** It existed only so iOS could receive Web Push from the Home Screen. Without push it has no purpose, so it is removed (Q6).
- **Hooking the Failed notification inside `_finalize_pivot`.** That would hide a side effect in a status writer that has no generation argument. The call site, which already holds `generation`, is clearer.

## Risks and mitigations

- **Data loss:** migration `0002` drops `notifications_pushsubscription`. Existing Web Push devices stop. The data is useless without pywebpush. Note it in the change doc; users re-enable through ntfy. The migration is irreversible: its rollback is restoring `0001` by re-adding the table empty.
- **ntfy.sh limits:**
  - 250 messages per day per IP on the free tier, and 60 burst requests then 1 every 5 s.
  - Per-user topics times per-case Failed notifications can exceed this on a big night run. For example, 100 failed cases times 3 members is 300.
  - Mitigations:
    - a 429 stops the batch with one warning, so there is no retry storm;
    - Failed is lower priority and per-user opt-out;
    - `example.env` and the docs recommend a self-hosted server or an ntfy.sh account token.
  - Q3 (summary instead of per-case) is the bigger lever if this bites.
- **Topic secrecy:** anyone with the topic on public ntfy.sh can read the notifications (project name, case title, ask or verdict excerpt) and spam the user. Mitigations:
  - a 192-bit server-generated topic, shown only to its owner;
  - never logged, never in task arguments, not in the admin;
  - Regenerate;
  - self-host with deny-all recommended.
  
  Verdict summaries can contain app data, so the body is truncated to 300 characters.
- **`NTFY_SERVER_URL` defaults to public ntfy.sh.** Nothing is sent until a user turns notifications on, and the page says where messages go.
- **Token leakage:** the token is sent only as a header. httpx does not log headers, and our logs record only status and exception type. A non-https server URL with a token set logs a startup-time warning in `ntfy_config` without the value.
- **Unregistered task name:** at deploy, `send_push` messages still in the broker are unregistered and dropped with an error log. That is acceptable, because they are transient.
- **N+1:** recipient selection is one query, the send-side lookup is one query, and the Failed entry point is one query. No per-user queries.
- **Blocking:** all HTTP happens in the `notify` worker, and none in requests or the execution worker. The execution worker only does one select and one `delay`.
- **XSS:** the topic, titles and URLs are autoescaped. The ntfy `markdown` flag is not set, so agent text renders as plain text in the apps.
- **Open redirect or phishing:** `click` is always `SITE_URL` plus a `reverse()` path, never user input.

## Implementation steps

Steps 2a, 2b and 2c touch disjoint files and can run as parallel sub-agents after step 1. Step 3 is the orchestrator's integration pass.

1. **Shared foundation** (orchestrator, before the sub-agents). Files:
   - `notifications/models.py` (`NotificationPreference`);
   - `notifications/migrations/0002_ntfy.py`;
   - `notifications/exceptions.py`;
   - `notifications/services/notification_message.py`, `ntfy_config.py` and `site_url.py`;
   - `auto_tester/settings.py` (NTFY_*, SITE_URL, task route, remove WEBPUSH_*) and `example.env`;
   - `requirements.txt` (drop pywebpush);
   - delete `stubs/pywebpush.pyi`.
   
   Freeze these interfaces:
   - the task signature `send_notification(user_ids, kind, title, body, path)`;
   - `recipient_user_ids(project_id, kind)`;
   - `NotificationKind`, `NotificationMessage` and `PreferenceView`.
2. **Parallel sub-agents:**
   - **2a, delivery.** `notifications/services/send_notification.py`, `notifications/tasks.py`. Delete `services/send_push.py`, `endpoint_policy.py` and `push_message.py`.
   - **2b, page.** `notifications/services/preferences.py` and `test_notification.py`; `notifications/forms.py`, `views.py`, `urls.py` and `admin.py`; `templates/notifications/settings.html`; `static/js/punk-hazard.js` (removals only); `static/css/punk-hazard.css`; `templates/base.html`. Delete `services/subscriptions.py`, `push_config.py` and `test_push.py`; `templates/notifications/sw.js` and `manifest.webmanifest`; and the three PWA icons.
   - **2c, project entry points.** `projects/case_help_notifications.py`; new `projects/case_failed_notifications.py`; `projects/services.py` (the import plus the call after `_finalize_pivot` only).
3. **Integrate** (orchestrator):
   - `auto_tester/urls.py` comment and `CLAUDE.md`;
   - grep for `push`, `webpush`, `PushSubscription`, `sw.js`, `manifest` and `pushDevice` leftovers;
   - `python manage.py makemigrations --check`, `manage.py check` and `pre-commit run --all-files`.
4. Review and verification loop, then `docs/changed/2026-10-24-ntfy-notifications.md`.

## Verification

Manual checks; this project has no tests.

1. `pre-commit run --all-files` is clean (strict mypy, with no pywebpush stub left). `python manage.py check` and `makemigrations --check` pass, and `migrate` drops the old table and creates the new one.
2. With `NTFY_SERVER_URL=` empty, the page shows the not-configured alert, and `notify_case_needs_help` and `notify_case_failed` return without enqueuing (check the worker log).
3. With `NTFY_SERVER_URL=https://ntfy.sh` and `SITE_URL` set:
   - turn notifications on, and the topic appears;
   - subscribe through "Open in ntfy" on Android and by hand on iOS or desktop;
   - send a test, and it arrives with ✅ and opens `/notifications/`;
   - a second test within 30 s shows the throttle message.
4. Regenerate: the old topic gets nothing (publish a test), and the new topic gets the test.
5. A Need help pause on an Attended run sends a notification with 🆘, priority high and the case-page click. Run the Night fallback too, for the "Auto-fix could not fix it" wording.
6. An agent-Failed case sends "Failed · <project>" with ❌, default priority and the case-page click. A Passed, Blocked or Cancelled case, or "Mark failed", sends nothing.
7. Stale generation: Resume or Re-run while an old worker finalizes, or simulate it in a rolled-back `manage.py shell` by calling `notify_case_failed` with an old generation. Nothing is enqueued.
8. Untick "Failed" and the Failed notification stops while Need help still arrives. Turn off and nothing arrives.
9. Grep the worker and web logs, and the Flower task arguments, for the topic and the token: no hits.
10. Point `NTFY_SERVER_URL` at an unreachable host. The task finishes within about `NTFY_REQUEST_TIMEOUT`, logs status-only warnings, and no retries appear.
11. View page source: no manifest or service worker. DevTools shows no new SW registration.
12. Run the `reviewer` and `verifier` agents in parallel on the diff, with this plan and the original requests.

## Open questions

- **Q1. Topic scope.** One topic per user (each person gets their own random topic, chooses their kinds and can regenerate alone), or one topic per project (shared by members, with fewer messages against ntfy.sh limits, but a shared secret and no per-person choices)? *Assumption in this draft: per user.*
- **Q2. Should Blocked cases notify too?** *Assumption: no. Blocked is an infrastructure state (runner outdated, errors), not a verdict on the app, and it is already visible on the run page.*
- **Q3. Failed: per case, or one summary when the run finishes?** *Assumption: per case only.* A run-finished summary ("Run X done: 12 passed, 3 failed") is not cheap today. `_update_test_run_status_if_needed` flips to DONE without a lock or conditional update, so two finishing workers could both notify. Doing it safely means changing that transition into a conditional `UPDATE ... WHERE status=STARTED`, which is better as its own small follow-up plan.
- **Q4. QR code** on the Notifications page for scanning the subscribe link from a phone. It needs a new dependency (for example `segno`, pure Python). *Assumption: no QR; the `ntfy://` link and copy buttons are enough.*
- **Q5. Per-user kind choice.** *Assumption: yes, two checkboxes (Need help, Failed), both on by default when notifications are turned on.*
- **Q6. PWA leftovers.** *Assumption: remove the manifest, `apple-touch-icon` and the three icons that served only the manifest or service worker, and keep `theme-color`.* Should the app stay installable to the Home Screen for other reasons?
- **Q7. `NTFY_SERVER_URL` default.** The request says `https://ntfy.sh`, which means a fresh install can send to the public server as soon as a user opts in. *Assumption: keep `https://ntfy.sh` as requested; the page and `example.env` say where messages go.* Would you rather default to empty (off until configured)?
- **Q8. `SITE_URL`.** A new setting is needed for click links, because Celery has no request. *Assumption: add `SITE_URL` (empty means no click link) rather than deriving it from `CSRF_TRUSTED_ORIGINS`.* What value should production use (`https://sench.remotereps.com`)?

## Changelog

- 2026-10-24: created. Extended the same day with Failed case notifications (`notify_case_failed`), per-user kind choices, and Q2, Q3 and Q5, on the user's added requirement.
- 2026-10-24: Q1–Q8 answered with the defaults (production SITE_URL `https://sench.remotereps.com`, set in `.env`; the setting defaults to empty). Status In progress.
