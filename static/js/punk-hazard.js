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
   * Check / Uncheck / Re-run / Send to TestRail / Needs re-test selection for
   * a run's case results table (templates/projects/test_run_detail.html). A
   * `bulkSelect` over the de-duplicated union of the five id lists (check and
   * re-run ids overlap on a finished run; send ids are failed, unsent cases,
   * so also check or uncheck ids; re-test ids are every case not already
   * marked), plus `toCheck` / `toUncheck` / `toRerun` / `toSend` /
   * `toRetest`: the selected ids each action applies to. `rerunIds` is empty
   * unless the run is Done or Cancelled; `sendIds` is empty unless TestRail
   * is configured and the run has cases with a source TestRail run;
   * `retestIds` is empty for a Draft run. Getters are copied as descriptors
   * so they stay live.
   */
  Alpine.data("runCaseChecks", (checkIds = [], uncheckIds = [], rerunIds = [], sendIds = [], retestIds = []) =>
    Object.defineProperties(
      bulkSelectState([...new Set([...checkIds, ...uncheckIds, ...rerunIds, ...sendIds, ...retestIds])]),
      Object.getOwnPropertyDescriptors({
        checkIds,
        uncheckIds,
        rerunIds,
        sendIds,
        retestIds,

        get toCheck() {
          return this.selected.filter((id) => this.checkIds.includes(id));
        },

        get toUncheck() {
          return this.selected.filter((id) => this.uncheckIds.includes(id));
        },

        get toRerun() {
          return this.selected.filter((id) => this.rerunIds.includes(id));
        },

        get toSend() {
          return this.selected.filter((id) => this.sendIds.includes(id));
        },

        get toRetest() {
          return this.selected.filter((id) => this.retestIds.includes(id));
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
   * run choices belong to the chosen projects, so a new choice clears the run
   * and re-submits the form. Pressing Apply with the dropdown open and changed
   * submits twice (button, then the close); the second, with the run cleared,
   * wins, which is right after a project change.
   */
  Alpine.data("failureFilters", () => ({
    projectsChanged(event) {
      const form = event.target.closest("form");
      if (form.elements.run) {
        form.elements.run.value = "";
      }
      form.requestSubmit();
    },
  }));

  /**
   * A dropdown of checkboxes (templates/partials/multi_select_field.html).
   * The summary shows `allLabel` when nothing is checked, the one checked
   * label, or "N chosen". Closing it after a change dispatches
   * `multi-select-changed`.
   */
  Alpine.data("multiSelect", (allLabel) => ({
    summary: allLabel,
    dirty: false,
    init() {
      this.refresh();
    },
    refresh() {
      const checked = this.$root.querySelectorAll("input[type=checkbox]:checked");
      if (checked.length === 0) {
        this.summary = allLabel;
      } else if (checked.length === 1) {
        const label = checked[0].closest("label").querySelector("[data-choice-label]");
        this.summary = label ? label.textContent.trim() : "1 chosen";
      } else {
        this.summary = `${checked.length} chosen`;
      }
    },
    changed() {
      this.dirty = true;
      this.refresh();
    },
    closed() {
      if (this.dirty) {
        this.dirty = false;
        this.$dispatch("multi-select-changed");
      }
    },
  }));

  /**
   * Live extraction status on the Knowledge base pages
   * (templates/knowledge_base/list.html and item.html). Opens `wsPath`
   * (ws/projects/<id>/knowledge-base/), reconnects with backoff, and on every
   * `{"type": "item", "item": {id, status, status_label, warning, error,
   * page_count, …}}` updates each `[data-kb-item="<id>"]` element below the
   * component: its `[data-kb-status]` badge, `[data-kb-warning]` /
   * `[data-kb-error]` notes (hidden when empty), `[data-kb-pages]` count, and
   * shows `[data-kb-ready-hint="<id>"]` once the item is ready. Text only
   * (textContent), never HTML. Must match _status_badge.html's class map.
   */
  const KB_STATUS_CLASSES = {
    pending: "badge-draft",
    extracting: "badge-running",
    ready: "badge-passed",
    failed: "badge-failed",
  };

  Alpine.data("knowledgeStatus", (wsPath) => ({
    socket: null,
    retryDelay: 1000,
    stopped: false,
    root: null,

    init() {
      this.root = this.$el;
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
      const socket = new WebSocket(window.punkHazard.wsUrl(wsPath));
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
        if (data.type === "item" && data.item && Number.isInteger(data.item.id)) {
          this.apply(data.item);
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

    apply(item) {
      const status = String(item.status || "");
      this.root.querySelectorAll(`[data-kb-item="${item.id}"]`).forEach((row) => {
        const badge = row.querySelector("[data-kb-status]");
        if (badge && KB_STATUS_CLASSES[status]) {
          Object.values(KB_STATUS_CLASSES).forEach((cls) => badge.classList.remove(cls));
          badge.classList.add(KB_STATUS_CLASSES[status]);
          badge.textContent = String(item.status_label || status);
        }
        for (const key of ["warning", "error"]) {
          const note = row.querySelector(`[data-kb-${key}]`);
          if (note) {
            const text = String(item[key] || "");
            note.textContent = text;
            note.hidden = text === "";
          }
        }
        const pages = row.querySelector("[data-kb-pages]");
        if (pages && Number.isInteger(item.page_count)) {
          pages.textContent = item.page_count > 0 ? String(item.page_count) : "—";
        }
      });
      if (status === "ready") {
        this.root.querySelectorAll(`[data-kb-ready-hint="${item.id}"]`).forEach((hint) => {
          hint.hidden = false;
        });
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
 * page's `extra_scripts` block (live run/case pages, runner status,
 * uploads progress, …). Builds a same-origin ws:// or wss:// URL from a
 * path such as `/ws/runners/3/status/`.
 */
window.punkHazard = {
  wsUrl(path) {
    const scheme = location.protocol === "https:" ? "wss://" : "ws://";
    return scheme + location.host + path;
  },

  /**
   * Live status of one Test Runner (`runnerId`: `/ws/runners/<id>/status/`,
   * run card, setup chat header, runner page) or of all the viewer's own
   * runners (`runnerId` null: `/ws/runners/status/`, the Test Runners list).
   * Calls `onMessage(data)` with each parsed message:
   * - `{type: "runner.status", runner_id, name, connected, machine_state,
   *   system_info, client_version, capabilities, last_connected_at,
   *   device_state, device_detail, device_reported_at}`
   *   (`runners.services.runner_status.RunnerStatusEvent`; the first one
   *   arrives on connect for a single runner);
   * - `{type: "runner_update", runner_id, runner_update}` (single runner
   *   only; `projects.runner_update_offer.RunnerUpdatePayload`);
   * - `{type: "visual_engine", visual_engine}` (the global engine).
   * Reconnects with a capped back-off; returns `{ close() }`. Runner-provided
   * strings (name, system info, device detail): bind with x-text only.
   */
  runnerStatusSocket(runnerId, onMessage) {
    const path = runnerId == null ? "/ws/runners/status/" : "/ws/runners/" + Number(runnerId) + "/status/";
    let socket = null;
    let closed = false;
    let delay = 1000;
    const open = () => {
      socket = new WebSocket(this.wsUrl(path));
      socket.onopen = () => { delay = 1000; };
      socket.onmessage = (event) => {
        let data;
        try {
          data = JSON.parse(event.data);
        } catch (error) {
          return;
        }
        onMessage(data);
      };
      socket.onclose = () => {
        if (closed) return;
        setTimeout(open, delay);
        delay = Math.min(delay * 2, 30000);
      };
    };
    open();
    return {
      close() {
        closed = true;
        if (socket) socket.close();
      },
    };
  },

  /**
   * The phone or tablet a Test Runner drives, from its `system_info` and
   * `capabilities` (docs/plan/feat-mobile-device-runner.md, D4):
   * `{ label, deviceId }`, or null for a desktop runner. Mirrors
   * `controller_client.protocol.parse_device_info` and the
   * `runners/device_label.py` (`runner_device_label`). Runner-provided:
   * bind with x-text only.
   */
  runnerDevice(systemInfo, capabilities) {
    const info = systemInfo || {};
    if (info.target !== "android" && info.target !== "ios") return null;
    if (!Array.isArray(capabilities) || !capabilities.includes("mobile_device_v1")) return null;
    const value = (key) => {
      const raw = info[key];
      if (typeof raw !== "string" && typeof raw !== "number") return "";
      const text = String(raw).trim();
      return text.length > 100 ? "" : text;
    };
    const platform = value("device_platform");
    const osVersion = value("device_os_version");
    const noun = platform === "iPadOS" ? "iPad" : info.target === "ios" ? "iPhone" : "Android phone";
    const osName = platform || (info.target === "ios" ? "iOS" : "Android");
    const kind = { physical: "real device", emulator: "emulator", simulator: "simulator" }[value("device_kind")] || "";
    const parts = [noun, value("device_model") || value("device_name"), osVersion ? osName + " " + osVersion : "", kind];
    return { label: parts.filter(Boolean).join(" · "), deviceId: value("device_id") };
  },

  /**
   * Whether a phone runner's device holds the machine badge (D6), as
   * `runners.machine_state.runner_machine_state` / `device_offline` decide: "" (it does not), "starting" (its session is
   * being created) or "device_offline" (any other state, or READY with no
   * status within `staleSeconds`). `now` is a reactive clock in ms so a
   * status that goes stale updates the badge.
   */
  deviceMachineState(device, state, reportedAt, staleSeconds, now) {
    if (!device || !state) return "";
    if (state === "starting") return "starting";
    if (state !== "ready") return "device_offline";
    return this.deviceStale(state, reportedAt, staleSeconds, now) ? "device_offline" : "";
  },

  // READY, but its last status is missing or older than `staleSeconds`.
  deviceStale(state, reportedAt, staleSeconds, now) {
    if (state !== "ready") return false;
    if (!reportedAt) return true;
    const reported = new Date(reportedAt).getTime();
    return Number.isNaN(reported) || now - reported > staleSeconds * 1000;
  },
};
