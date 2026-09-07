# Partials reference

Notes that don't fit as a one-line comment at the top of a partial file.

## Dialog convention

Every modal on the site — confirmations, edit forms, the screenshot
lightbox — follows the same shape, built on Alpine's `dialog()` component
(`static/js/punk-hazard.js`) and a browser `CustomEvent` dispatched by
name. There is no shared parent state and no prop-drilling: a trigger and
its dialog only need to agree on one string, the event name.

**Why events instead of a shared `x-data` var:** Django templates have no
slot mechanism, so a dialog's trigger (a table row's ⋯ menu item, a page
header button, …) is very often rendered somewhere structurally distant
from the dialog markup itself (e.g. one dialog shared by every row in a
table). Wrapping trigger + dialog in one common `x-data` scope would force
awkward DOM nesting just to share a boolean. Dispatching a named
`window` event decouples the two completely.

**The dialog itself** (`.dialog-backdrop` wrapping `.dialog`):

```html
<div
  class="dialog-backdrop"
  x-data="dialog()"
  x-show="open"
  x-cloak
  x-on:open-my-dialog.window="show()"
  x-on:keydown.escape.window="close()"
  x-trap.noscroll="open"
  @click.self="close()"
>
  <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="my-dialog-title">
    <div class="card-header">
      <h3 id="my-dialog-title">…</h3>
      <button class="icon-btn" type="button" aria-label="Close" @click="close()">✕</button>
    </div>
    … body …
    <div class="dialog-actions">
      <button class="btn btn-ghost" type="button" @click="close()">Cancel</button>
      <button class="btn btn-primary" type="submit">Save</button>
    </div>
  </div>
</div>
```

Use `role="alertdialog"` instead of `role="dialog"` for a confirmation
(irreversible action, nothing to fill in) — see `confirm_dialog.html`.
Add `.dialog-lg` alongside `.dialog` for content-heavy dialogs (the
screenshot lightbox).

**Any trigger, anywhere inside the `.app` shell** (which carries the root
`x-data` in `layouts/app.html`, so `$dispatch` is available everywhere
under it):

```html
<button type="button" @click="$dispatch('open-my-dialog')">Edit</button>
```

**`confirm_dialog.html`** is the one ready-made include for the common
case — a title, a body paragraph, a Cancel button, and a single submit
button that POSTs to an action URL. See the contract comment at the top
of that file. For anything else (a form with real fields, the screenshot
lightbox's prev/next controls, …), copy the markup pattern above directly
into the page template — it's a handful of attributes, not worth
abstracting behind parameters that would only fit one call site.

Each dialog on a page needs a **unique `dialog_id`** (or event-name
suffix, if you're not using `confirm_dialog.html`) — e.g.
`archive-project-{{ project.id }}` when a dialog is rendered once per row
in a loop.

## `advanced_diagnostics.html` — expected Alpine scope

This partial reads variables from an ancestor `x-data` scope; it does not
declare them itself. The page that includes it is expected to define an
agent-status WebSocket component (ported from the current
`detail.html` in a later phase) exposing:

```js
{
  agentConnected: boolean,
  clientVersion: string,
  capabilities: string[],
  omniparserStatus: {
    state: string,
    message: string,
    device: string,
    weights_dir: string,
    phase: string,
    load_seconds: number,
  },
  systemInfo: object,
  lastConnectedAt: string | null,
}
```

Until that component exists, wrap `{% include "partials/advanced_diagnostics.html" %}`
in a scope providing at least empty defaults (`x-data="{ clientVersion: '', capabilities: [], omniparserStatus: {} }"`)
so the `x-text` bindings don't error against `undefined`.
