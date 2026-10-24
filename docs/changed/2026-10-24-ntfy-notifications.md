# Notifications via ntfy; failed cases notify too

Date: 2026-10-24
Plan: docs/plan/refactor-ntfy-notifications.md

## What changed
- `notifications`:
  - Web Push is replaced by ntfy. `NotificationPreference` holds one per user: a server-generated random topic, on/off, and the kinds Need help / Failed.
  - `services/send_notification.py` publishes JSON to `NTFY_SERVER_URL` with `httpx`, using at most 4 parallel requests and no retries. A 429 or any transport error stops the batch.
  - `tasks.send_notification` runs on the `notify` queue.
  - New services: `preferences`, `test_notification`, `recipients`, `ntfy_config`, `site_url` and `notification_message`.
- Notifications page `/notifications/`:
  - shows the topic and server with copy buttons, and links to open the ntfy app and the web client;
  - has Regenerate, Turn on/off, the kind checkboxes, and a test button throttled to one every 30 s.
- `projects/case_help_notifications.notify_case_needs_help`: same contract, now sent through ntfy.
- New `projects/case_failed_notifications.notify_case_failed(pivot_id, *, generation)`. It is called after `_finalize_pivot` when the agent's verdict is Failed, and only for the current generation. Mark failed does not notify.
- Removed:
  - `pywebpush` and its stub, the `WEBPUSH_*` settings and `PushSubscription`;
  - `/sw.js`, the manifest, the PWA icons and the apple tags;
  - the push JS and CSS.

## Why
The user prefers the ntfy app over browser Web Push: there are no VAPID keys or service worker, and it works on iPhone without Add to Home Screen. Failed cases now notify too.

## Behaviour and contract changes
- URLs:
  - added `notifications:{enable,disable,regenerate,kinds}`;
  - kept `notifications:settings` and `notifications:test`;
  - removed `subscribe`, `unsubscribe`, `service_worker` and `manifest`.
- Settings:
  - `NTFY_SERVER_URL` (default https://ntfy.sh; empty disables), `NTFY_ACCESS_TOKEN`, `NTFY_TOPIC_PREFIX`, `NTFY_REQUEST_TIMEOUT`;
  - `SITE_URL` for click links (production: https://sench.remotereps.com in `.env`);
  - `WEBPUSH_*` removed.
- Migration `notifications.0002` drops `PushSubscription`. Existing Web Push devices are lost, so users turn on ntfy on the page.
- Deploy: run `migrate` and restart Daphne and every Celery worker, including `notify`.

## Notes for future changes
- A topic is a password on ntfy.sh. Never log it, never put it in task args, and show it only to its owner.
- The ntfy.sh free tier allows about 250 messages a day per server IP. For big night runs, self-host ntfy or use an account token (see `example.env`).
- Not considered: a run-finished summary. It needs the DONE transition made conditional first.
- Not exercised against a real ntfy server or in a browser yet.
