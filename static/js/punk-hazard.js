/*
 * Punk Hazard — shared Alpine.js components.
 *
 * Plain ES2020, no build step. Registered on `alpine:init` so they are
 * available to every template's `x-data="…"` attribute before Alpine
 * scans the DOM. Page-specific components (WebSocket-driven live views)
 * stay in each template's `extra_scripts` block — this file only holds
 * components reused across pages.
 *
 * See templates/partials/README.md for the dialog-opening convention
 * (`dialog()` + `$dispatch('open-<id>')`) that `dialog()` below implements.
 */

document.addEventListener("alpine:init", () => {
  /**
   * A single dialog instance. Pair it with a `.dialog-backdrop` element:
   *
   *   <div class="dialog-backdrop"
   *        x-data="dialog()"
   *        x-show="open"
   *        x-cloak
   *        x-on:open-my-dialog.window="show()"
   *        x-on:keydown.escape.window="close()"
   *        x-trap.noscroll="open"
   *        @click.self="close()">
   *     <div class="dialog" role="dialog" aria-modal="true">…</div>
   *   </div>
   *
   * Any element inside the app shell can open it, with no shared state,
   * by dispatching the matching event:
   *
   *   <button @click="$dispatch('open-my-dialog')">Edit</button>
   */
  Alpine.data("dialog", () => ({
    open: false,
    show() {
      this.open = true;
    },
    close() {
      this.open = false;
    },
  }));

  /**
   * Tablist for the OS-specific Test Runner setup instructions
   * (templates/partials/os_setup_tabs.html). `initial` is the id of the
   * tab selected on load.
   */
  Alpine.data("osTabs", (initial = "unix") => ({
    active: initial,

    select(id) {
      this.active = id;
    },

    isActive(id) {
      return this.active === id;
    },

    // Arrow-key roving focus across the tablist, per the tabs ARIA pattern.
    onKeydown(event, ids) {
      const currentIndex = ids.indexOf(this.active);
      if (currentIndex === -1) {
        return;
      }

      let nextIndex = null;
      if (event.key === "ArrowRight") {
        nextIndex = (currentIndex + 1) % ids.length;
      } else if (event.key === "ArrowLeft") {
        nextIndex = (currentIndex - 1 + ids.length) % ids.length;
      } else {
        return;
      }

      event.preventDefault();
      const nextId = ids[nextIndex];
      this.select(nextId);
      this.$nextTick(() => this.$refs[`tab-${nextId}`]?.focus());
    },
  }));

  /**
   * Checkbox selection for a table's bulk-action bar
   * (e.g. templates/projects/test_cases.html). `allIds` is the full list
   * of selectable row ids on the current page.
   */
  const bulkSelectState = (allIds = []) => ({
    allIds,
    selected: [],

    toggle(id) {
      const index = this.selected.indexOf(id);
      if (index === -1) {
        this.selected.push(id);
      } else {
        this.selected.splice(index, 1);
      }
    },

    toggleAll() {
      this.selected = this.allSelected ? [] : [...this.allIds];
    },

    get allSelected() {
      return this.allIds.length > 0 && this.selected.length === this.allIds.length;
    },

    get count() {
      return this.selected.length;
    },

    clear() {
      this.selected = [];
    },
  });

  Alpine.data("bulkSelect", bulkSelectState);

  /**
   * Check / Uncheck / Re-run selection for a run's case results table
   * (templates/projects/test_run_detail.html). A `bulkSelect` over the
   * de-duplicated union of the three id lists (check and re-run ids
   * overlap on a finished run), plus `toCheck` / `toUncheck` / `toRerun`:
   * the selected ids each action applies to. `rerunIds` is empty unless
   * the run is Done or Cancelled. Getters are copied as descriptors so
   * they stay live.
   */
  Alpine.data("runCaseChecks", (checkIds = [], uncheckIds = [], rerunIds = []) =>
    Object.defineProperties(
      bulkSelectState([...new Set([...checkIds, ...uncheckIds, ...rerunIds])]),
      Object.getOwnPropertyDescriptors({
        checkIds,
        uncheckIds,
        rerunIds,

        get toCheck() {
          return this.selected.filter((id) => this.checkIds.includes(id));
        },

        get toUncheck() {
          return this.selected.filter((id) => this.uncheckIds.includes(id));
        },

        get toRerun() {
          return this.selected.filter((id) => this.rerunIds.includes(id));
        },
      }),
    ),
  );

  /**
   * Copy-to-clipboard button. `copied` flips back to false after 1.5s so
   * a "Copied!" label can be shown briefly via `x-show="copied"`.
   */
  Alpine.data("clipboard", () => ({
    copied: false,

    async copy(text) {
      try {
        await navigator.clipboard.writeText(text);
        this.copied = true;
        setTimeout(() => {
          this.copied = false;
        }, 1500);
      } catch {
        // Clipboard API unavailable or permission denied — the button
        // simply won't confirm; nothing else to recover here.
      }
    },
  }));

  /** Sidebar-replacement nav panel shown on narrow viewports. */
  Alpine.data("mobileNav", () => ({
    open: false,

    toggle() {
      this.open = !this.open;
    },

    close() {
      this.open = false;
    },
  }));

  /** Dismiss control for a single `.alert` (templates/partials/messages.html). */
  Alpine.data("dismissible", () => ({
    shown: true,

    dismiss() {
      this.shown = false;
    },
  }));

  /**
   * Web Push for this browser (templates/notifications/settings.html).
   * Reads its configuration from data-* attributes on its element:
   * data-public-key (VAPID application server key, base64url),
   * data-subscribe-url, data-unsubscribe-url, data-csrf-token.
   * The subscription's endpoint and keys go only to the server.
   */
  Alpine.data("pushDevice", () => ({
    supported: false,
    permission: "default",
    subscribed: false,
    busy: false,
    error: "",
    showIosHint: false,
    config: {},

    async init() {
      this.config = { ...this.$root.dataset };
      this.supported = punkHazard.pushSupported();
      // iOS Safari exposes Web Push only to Home Screen web apps (16.4+).
      this.showIosHint = punkHazard.isIos() && !punkHazard.isStandalone();
      if (!this.supported) {
        return;
      }
      this.permission = Notification.permission;
      try {
        const registration = await punkHazard.registerServiceWorker();
        const subscription = await registration.pushManager.getSubscription();
        this.subscribed = subscription !== null;
      } catch {
        this.subscribed = false;
      }
    },

    async post(url, fields) {
      return fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRFToken": this.config.csrfToken },
        body: new URLSearchParams(fields),
      });
    },

    async enable() {
      this.busy = true;
      this.error = "";
      let subscription = null;
      try {
        this.permission = await Notification.requestPermission();
        if (this.permission !== "granted") {
          return;
        }
        const registration = await punkHazard.registerServiceWorker();
        subscription = await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: punkHazard.base64UrlToBytes(this.config.publicKey),
        });
        const json = subscription.toJSON();
        const response = await this.post(this.config.subscribeUrl, {
          endpoint: json.endpoint,
          p256dh: json.keys.p256dh,
          auth: json.keys.auth,
        });
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new Error(body.error || "The server did not accept this device.");
        }
        window.location.reload();
      } catch (err) {
        // Do not leave a browser subscription the server does not know about.
        await subscription?.unsubscribe().catch(() => {});
        this.subscribed = false;
        this.error = err instanceof Error && err.message ? err.message : "Could not turn on notifications.";
      } finally {
        this.busy = false;
      }
    },

    async disable() {
      this.busy = true;
      this.error = "";
      try {
        const registration = await punkHazard.registerServiceWorker();
        const subscription = await registration.pushManager.getSubscription();
        if (subscription !== null) {
          const endpoint = subscription.endpoint;
          await subscription.unsubscribe();
          await this.post(this.config.unsubscribeUrl, { endpoint });
        }
        window.location.reload();
      } catch {
        this.error = "Could not turn off notifications. Try again.";
      } finally {
        this.busy = false;
      }
    },
  }));

  /** The ⋯ row-action dropdown (templates/partials's `.row-menu` markup). */
  Alpine.data("rowMenu", () => ({
    open: false,

    toggle() {
      this.open = !this.open;
    },

    close() {
      this.open = false;
    },
  }));
});

/**
 * Shared helper for every WebSocket-driven Alpine component defined in a
 * page's `extra_scripts` block (live run/case pages, agent-status,
 * uploads progress, …). Builds a same-origin ws:// or wss:// URL from a
 * path such as `/ws/projects/3/agent-status/`.
 */
window.punkHazard = {
  wsUrl(path) {
    const scheme = location.protocol === "https:" ? "wss://" : "ws://";
    return scheme + location.host + path;
  },

  /** Web Push needs a secure context, a service worker and the Push API. */
  pushSupported() {
    return (
      window.isSecureContext &&
      "serviceWorker" in navigator &&
      "PushManager" in window &&
      "Notification" in window
    );
  },

  isIos() {
    return (
      /iPad|iPhone|iPod/.test(navigator.userAgent) ||
      // iPadOS reports itself as a Mac.
      (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)
    );
  },

  isStandalone() {
    return navigator.standalone === true || window.matchMedia("(display-mode: standalone)").matches;
  },

  /** The site-wide service worker (notifications.views.service_worker). */
  registerServiceWorker() {
    return navigator.serviceWorker.register("/sw.js", { scope: "/" });
  },

  /** VAPID public key (base64url) to the Uint8Array pushManager.subscribe wants. */
  base64UrlToBytes(value) {
    const padded = (value + "=".repeat((4 - (value.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
    const raw = atob(padded);
    return Uint8Array.from(raw, (char) => char.charCodeAt(0));
  },
};

// Register the service worker on every page so notification clicks are
// handled and the push subscription stays bound to it.
if ("serviceWorker" in navigator && window.isSecureContext) {
  window.addEventListener("load", () => {
    window.punkHazard.registerServiceWorker().catch(() => {});
  });
}
