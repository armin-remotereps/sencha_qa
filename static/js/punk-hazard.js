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
   * The app shell's attention socket (templates/layouts/app.html, one per
   * tab). Opens ws/attention/, reconnects with backoff, and on every
   * `{"type": "attention", "count": N}` updates the nav badges
   * ([data-attention-badge]), the `(N) ` tab-title prefix and the dashboard
   * subtitle; on the dashboard it also re-fetches the live cards from
   * #attention-card's data-fragment-url and swaps each of LIVE_CARD_IDS in
   * by id (server-rendered, escaped HTML; the cards hold no Alpine
   * directives, so they need no re-initialisation).
   */
  const LIVE_CARD_IDS = ["attention-card", "failed-widget"];

  Alpine.data("attentionLive", () => ({
    socket: null,
    retryDelay: 1000,
    stopped: false,
    fetchSeq: 0,
    baseTitle: "",

    init() {
      this.baseTitle = document.title.replace(/^\(\d+\) /, "");
      this.connect();
    },

    destroy() {
      this.stopped = true;
      if (this.socket) {
        this.socket.close();
      }
    },

    connect() {
      if (this.stopped) {
        return;
      }
      const socket = new WebSocket(window.punkHazard.wsUrl("/ws/attention/"));
      this.socket = socket;
      socket.onopen = () => {
        this.retryDelay = 1000;
      };
      socket.onmessage = (event) => {
        let data;
        try {
          data = JSON.parse(event.data);
        } catch {
          return;
        }
        if (data.type === "attention" && Number.isInteger(data.count)) {
          this.apply(data.count);
        }
      };
      socket.onclose = () => {
        if (this.stopped) {
          return;
        }
        setTimeout(() => this.connect(), this.retryDelay);
        this.retryDelay = Math.min(this.retryDelay * 2, 30000);
      };
    },

    apply(count) {
      document.title = (count > 0 ? `(${count}) ` : "") + this.baseTitle;
      document.querySelectorAll("[data-attention-badge]").forEach((badge) => {
        badge.hidden = count <= 0;
        const number = badge.querySelector("[data-attention-badge-count]");
        if (number) {
          number.textContent = String(count);
        }
      });
      const subtitle = document.querySelector("[data-attention-subtitle]");
      if (subtitle) {
        subtitle.hidden = count <= 0;
        subtitle.textContent =
          `${count} item${count === 1 ? "" : "s"} ` +
          `need${count === 1 ? "s" : ""} your attention`;
      }
      this.refreshCard();
    },

    async refreshCard() {
      const card = document.getElementById("attention-card");
      const url = card?.dataset.fragmentUrl;
      if (!url) {
        return;
      }
      const seq = ++this.fetchSeq;
      try {
        const response = await fetch(url, {
          credentials: "same-origin",
          headers: { Accept: "text/html" },
        });
        if (!response.ok || seq !== this.fetchSeq) {
          return;
        }
        const html = await response.text();
        if (seq !== this.fetchSeq) {
          return;
        }
        const template = document.createElement("template");
        template.innerHTML = html.trim();
        for (const id of LIVE_CARD_IDS) {
          const fresh = template.content.getElementById(id);
          const current = document.getElementById(id);
          if (fresh && current) {
            current.replaceWith(fresh);
          }
        }
      } catch {
        // Offline or the session ended: the next event retries.
      }
    },
  }));

  /**
   * The Failures page filter form (templates/dashboard/failed_cases.html):
   * run choices belong to one project, so a new project clears the run and
   * re-submits the form.
   */
  Alpine.data("failureFilters", () => ({
    projectChanged(event) {
      const form = event.target.form;
      if (form.elements.run) {
        form.elements.run.value = "";
      }
      form.requestSubmit();
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
};
