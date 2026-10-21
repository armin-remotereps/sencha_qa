# Connection key card on the Environment page

Date: 2026-10-22
Plan: docs/plan/feat-connection-key-card.md

## What changed
- `templates/partials/connection_key_card.html` (new): masked `CONTROLLER_API_KEY=<key>` line with reveal, "Copy .env line", "Copy key only", Regenerate (and its confirm dialog), plus steps to switch an installed Test Runner to this project.
- `templates/projects/environment.html`: includes the card after the setup section.
- `templates/partials/advanced_diagnostics.html`: key row, Regenerate and dialog removed; partial no longer needs `project`.
- `templates/partials/runner_download_card.html`: "no key to copy" replaced by a pointer to the Connection key card.

## Why
One downloaded Test Runner can serve several projects: edit the `CONTROLLER_API_KEY` line in `controller_client/.env` and restart. The key was hidden in Advanced diagnostics.

## Behaviour and contract changes
- Wizard step 4 (`setup_machine`) no longer shows the key or Regenerate (user chose Environment only); the ZIP still has the key baked in.
- Regenerate dialog wording: old key stops working for new connections (a connected runner is not forcibly disconnected).

## Notes for future changes
- The card owns the `regenerate-key` dialog id: include it at most once per page.
- The runner reads `.env` once at start-up; `--api-key` and an OS env var override it.
