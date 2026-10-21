# feat: Show the connection key on each project so one Test Runner can switch projects

Status: Done
Created: 2026-10-22
Request: Make each project's connection key (`Project.api_key`) easy to find and
copy, so a user who already has a Test Runner installed on a test machine can
point it at a different project by editing one line in `controller_client/.env`
and restarting it, instead of downloading and setting up a new ZIP per project.

## Context
- `Project.api_key` (`projects/models.py`, `secrets.token_urlsafe(32)`, so only
  `[A-Za-z0-9_-]`: safe unquoted in `.env` and in JS strings) is the Test Runner's
  connection key.
- The runner reads `CONTROLLER_API_KEY` (plus `CONTROLLER_HOST`/`CONTROLLER_PORT`)
  once at start-up from `controller_client/.env` via decouple
  (`controller_client/config.py::load_config`); a `--api-key` CLI flag overrides it.
  A running runner never re-reads `.env`, so a restart is required.
- The downloaded ZIP bakes a per-project `.env`
  (`projects/services.py::_generate_env_content`): host, port, key, and three
  tuning values. Host/port come from server settings and are identical for every
  project on one server.
- Today the key is only shown inside the collapsed "Advanced diagnostics"
  `<details>` (`templates/partials/advanced_diagnostics.html`) with reveal, Copy and
  Regenerate, and the hint "Only needed when support asks…". That partial is
  included by `templates/projects/environment.html` and
  `templates/projects/wizard/step_machine.html`; it also owns the
  `regenerate-key` confirm dialog.
- `templates/partials/runner_download_card.html` says "there is no key to copy",
  which becomes misleading.
- `projects:regenerate_api_key` (`projects/views.py::project_regenerate_api_key`)
  is `@project_membership_required` + `@require_POST` and redirects to
  `projects:environment`. Both pages are `@project_membership_required`.
- Reusable pieces: `clipboard()` Alpine component (`static/js/punk-hazard.js`),
  `.card`, `.card-header`, `.kv`, `.code-block`, `.hint`, `.btn-sm` in
  `static/css/punk-hazard.css`, `partials/confirm_dialog.html`.

## Goals
- A visible "Connection key" card on the Environment page, outside Advanced
  diagnostics: masked key with reveal toggle, Copy, and Regenerate.
- A copyable `.env` line `CONTROLLER_API_KEY=<key>` with a short instruction:
  open `controller_client/.env` on the test machine, replace the
  `CONTROLLER_API_KEY=` line, stop the Test Runner (Ctrl+C) and start it again.
- Same card on wizard step 4 (`setup_machine`), since creating a new project and
  reusing an already-installed runner is exactly this use case.
- No duplicate of the key in Advanced diagnostics.

## Non-goals
- No new views, URLs, forms, services, models or migrations.
- No change to the controller client, its protocol, or `_generate_env_content`.
- No multi-project support inside one runner process, no runner-side
  "switch project" command.
- No change to how Regenerate behaves (still disconnect-by-invalidation, redirect
  to Environment).

## Design
### Data model
None.

### Services
None. The template reads `project.api_key`, which both pages already have in
context (same as the existing partial).

### Pages and forms
| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `projects:environment` (existing) | GET | `@project_membership_required` | none | `projects/environment.html`, context unchanged (`project`, `machine_state`) | unchanged |
| `projects:setup_machine` (existing) | GET | `@project_membership_required` | none | `projects/wizard/step_machine.html`, context unchanged (`project`, `wizard_step`, `wizard_mode`, `active_nav`) | unchanged |
| `projects:regenerate_api_key` (existing) | POST | `@project_membership_required` | none (CSRF via confirm dialog) | redirect to Environment | unchanged |

New partial `templates/partials/connection_key_card.html`
- Context contract: `project` (key value and regenerate URL). Self-contained
  Alpine scopes (`x-data="{ revealed: false }"`, `clipboard()`); no dependence on
  the `environmentLive()` ancestor scope.
- Content:
  - Header "Connection key".
  - One-line explanation: "Identifies this project to the Test Runner. A runner
    already installed for another project can be switched to this one."
  - Key row: masked `••••••••` / revealed `{{ project.api_key }}` (auto-escaped),
    eye toggle (`aria-label` Show/Hide), Copy (`copy('{{ project.api_key|escapejs }}')`),
    Regenerate (`$dispatch('open-regenerate-key')`). Markup moved from
    `advanced_diagnostics.html`.
  - "Switch an installed Test Runner to this project" sub-section: numbered
    `.setup-steps` list:
    1. On the test machine, open `controller_client/.env`.
    2. Replace the `CONTROLLER_API_KEY=` line with: a `.code-block` showing
       `CONTROLLER_API_KEY=` followed by a masked placeholder until revealed, plus
       its own Copy button that copies `CONTROLLER_API_KEY={{ project.api_key|escapejs }}`.
    3. Stop the Test Runner (Ctrl+C in its window) and start it again with the
       same start command as before. It then connects to this project.
  - `.hint`: "Leave the other lines as they are. A runner connects to one project
    at a time; the project it leaves shows Not connected. Regenerating the key
    disconnects any runner still using the old one."
  - The `regenerate-key` `confirm_dialog.html` include moves here from
    `advanced_diagnostics.html` (dialog ids stay unique: the partial appears once
    per page).
- Host/port decision: the snippet contains only the `CONTROLLER_API_KEY` line.
  `CONTROLLER_HOST`/`CONTROLLER_PORT` are server-wide and already correct in any
  `.env` downloaded from this server; including them would invite users to
  overwrite them and adds a settings dependency to the page context for no gain.
  Moving a runner between servers is out of scope (download a fresh ZIP).

Edits to existing templates
- `templates/partials/advanced_diagnostics.html`: remove the Connection key `<dt>/<dd>`,
  the "Only needed when support asks…" hint and the regenerate dialog include.
  Update the header comment: it no longer needs `project` (includes may keep
  passing it; harmless).
- `templates/projects/environment.html`: include `partials/connection_key_card.html`
  with `project=project` inside the `setup-h` section, after the "Set it up" card
  and before Advanced diagnostics. Update the page comment.
- `templates/projects/wizard/step_machine.html`: include the same partial after
  "Set it up" (before "Connection status"). Update its comment.
- `templates/partials/runner_download_card.html`: replace "there is no key to copy"
  with wording like "The connection settings are already inside. Already have a
  Test Runner installed? Use the connection key below instead of downloading
  again."
- `templates/partials/README.md`: document the new partial's contract; adjust the
  advanced_diagnostics note if it mentions the key.

### Background work and WebSockets
None. Existing `regenerate_api_key` behaviour is unchanged.

### Settings / infrastructure
None.

### Data flow
Member opens Environment (or wizard step 4) -> view renders with `project` ->
partial renders masked key, server-escaped -> user reveals / copies the key or
the `.env` line (client-side `navigator.clipboard`) -> user edits
`controller_client/.env` on the test machine and restarts the runner -> runner
handshakes with the new key -> server resolves the project by `api_key` and marks
it connected (existing path).

## Alternatives considered
- Keep the key in Advanced diagnostics and just reword the hint: not discoverable,
  which is the complaint.
- Copy the full `.env` (host, port, key, tuning values): overwriting the whole file
  works too, but risks clobbering local edits (e.g. `CONTROLLER_CLEANUP_DIR`) and
  needs new context from settings. One-line replacement is simpler.
- Document `--api-key` CLI flag instead: works, but users start the runner through
  the documented command; editing `.env` matches the request.
- Inline the card in both page templates instead of a partial: duplicated markup
  across two pages; a partial follows the existing `templates/partials/` convention.

## Risks and mitigations
- Secret exposure: the key is already rendered to members today; visibility
  scope is unchanged (both pages behind `@project_membership_required`). The value
  is in the DOM even while masked (as today); masking only guards shoulder-surfing.
- XSS: HTML contexts use default autoescape, JS string contexts use `|escapejs`;
  no `|safe`. Key alphabet is URL-safe anyway.
- Regenerate from the wizard redirects to Environment and leaves the wizard
  (pre-existing behaviour, now more visible). See Q2.
- Duplicate dialog id if the partial were included twice on a page: it is included
  once per page; note this in the partial contract.

## Implementation steps
1. Create `templates/partials/connection_key_card.html` (move key row and
   regenerate dialog from Advanced diagnostics, add the switch instructions and
   `.env` line). If a small layout class is needed (e.g. key row alignment), add
   it to `static/css/punk-hazard.css`, no inline styles.
   (files: `templates/partials/connection_key_card.html`, possibly
   `static/css/punk-hazard.css`)
2. Strip the key and dialog from `templates/partials/advanced_diagnostics.html`;
   update its comment and `templates/partials/README.md`.
3. Include the card on `templates/projects/environment.html` and
   `templates/projects/wizard/step_machine.html`; update their comments.
4. Reword the hint in `templates/partials/runner_download_card.html`.
5. `pre-commit run --all-files`, `python manage.py check`.

All steps are template-only and tightly linked; implement directly, no sub-agents.

## Verification
- `pre-commit run --all-files` and `python manage.py check` clean.
- Environment page as a member: card visible without opening Advanced
  diagnostics; key masked by default; eye toggles; both Copy buttons put the exact
  key / `CONTROLLER_API_KEY=<key>` on the clipboard (paste to check, no quotes or
  whitespace); Regenerate opens the confirm dialog, POST regenerates and the page
  shows the new key and success message.
- Advanced diagnostics no longer shows the key or Regenerate; its other fields
  still render.
- Wizard step 4 shows the same card; no Alpine console errors on either page.
- Mobile width: card wraps cleanly; long key scrolls inside `.code-block`.
- End-to-end: with a runner connected to project A, paste project B's line into
  `controller_client/.env`, restart; B's Environment shows connected, A shows Not
  connected.
- Non-member gets the existing 403/404 on both pages (unchanged decorator).
- Run `reviewer` and `verifier` agents.

## Open questions
- Q1 Should the wizard step 4 page also show the card, or only Environment?
  (assumption: both, since a new project reusing an installed runner is the main
  use case.)
- Q2 Should Regenerate be offered on the wizard page too, given it redirects to
  Environment and leaves the wizard? (assumption: yes, same as today; hide it on
  the wizard via an include flag if the user prefers.)
- Q3 Copy snippet: only the `CONTROLLER_API_KEY=` line, or the full `.env`?
  (assumption: key line only; host/port are server-wide and already correct.)

## Answers (user, 2026-10-22)
- Q1: Environment only. The wizard does not include the card; Advanced
  diagnostics no longer shows the key, so the wizard shows no key (the ZIP it
  offers already has the key baked in). `runner_download_card.html` links to the
  Environment page for switching an installed runner.
- Q2: Regenerate on Environment only (it lives in the card).
- Q3: Key line only.

## Changelog
- 2026-10-22: created
- 2026-10-22: answers recorded; wizard include of the card dropped. The card
  shows the `.env` line in one code block with "Copy .env line" and "Copy key
  only" buttons instead of a separate key row. `templates/partials/README.md` not
  changed: partial contracts live in each partial's header comment, the README
  only holds notes that do not fit there.
- 2026-10-22: review pass 1 fixes: download-card link wording, regenerate dialog
  body (key stops working for new connections; no forced disconnect), card moved
  out of the setup `<section>`, `aria-pressed` on the reveal toggle, restart step
  notes `--api-key` / env var precedence. Pass 2 clean. Not done (optional nit):
  a `#connection-key` anchor so the download card's link scrolls on the
  Environment page itself; the card is directly below, so it adds little.
