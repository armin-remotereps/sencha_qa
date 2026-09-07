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
  Alpine.data("bulkSelect", (allIds = []) => ({
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
  }));

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
