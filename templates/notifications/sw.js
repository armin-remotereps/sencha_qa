{% load static %}/*
 * Punk Hazard service worker, served by notifications.views.service_worker at
 * /sw.js (scope "/"). It only shows Web Push notifications and opens their
 * page when clicked; it caches nothing.
 */

const DEFAULT_TITLE = "{{ brand_name|default:'Punk Hazard'|escapejs }}";
const ICON_URL = "{% static 'branding/punk-hazard-icon-192.png' %}";

/* A same-origin path for `raw`, or "/" for anything else (other origins,
 * javascript:, data:, protocol-relative "//host", ...). */
function sameOriginPath(raw) {
  if (typeof raw !== "string" || !raw.startsWith("/")) {
    return "/";
  }
  try {
    const url = new URL(raw, self.location.origin);
    if (url.origin !== self.location.origin) {
      return "/";
    }
    return url.pathname + url.search + url.hash;
  } catch {
    return "/";
  }
}

function asText(value, fallback) {
  return typeof value === "string" && value ? value : fallback;
}

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let payload = {};
  if (event.data) {
    try {
      payload = event.data.json() || {};
    } catch {
      payload = { body: event.data.text() };
    }
  }
  const title = asText(payload.title, DEFAULT_TITLE);
  const options = {
    body: asText(payload.body, ""),
    icon: ICON_URL,
    badge: ICON_URL,
    data: { url: sameOriginPath(payload.url) },
  };
  if (typeof payload.tag === "string" && payload.tag) {
    options.tag = payload.tag;
    // A newer notification with the same tag alerts again.
    options.renotify = true;
  }
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const path = sameOriginPath(event.notification.data && event.notification.data.url);
  const target = new URL(path, self.location.origin).href;

  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      for (const client of windows) {
        if (client.url === target && "focus" in client) {
          return client.focus();
        }
      }
      return self.clients.openWindow(target);
    })(),
  );
});
