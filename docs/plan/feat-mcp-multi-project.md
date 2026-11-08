# feat: One MCP connection for several projects

Status: Done
Created: 2026-11-08
Request: Today one Claude account can reach only one project through the MCP gateway. A claude.ai, Desktop or Cowork custom connector signs in with OAuth, and the consent page approves exactly one project (`McpGrant.project`), so that connector covers one project. Claude refuses a second connector with the same URL, so the user cannot add a second connector for another project either. Let one Claude account (and ChatGPT, Codex, Claude Code) work on several projects. Keep the existing per-project rules: write access, archive and Connection key regeneration end access to that project; actions are recorded as "a tester" (no-trace rule); Connection key bearers keep working, scoped to their one project.

## Context

- **Gateway**: `mcp_gateway` (`docs/changed/2026-11-07-controller-mcp.md`, `-mcp-oauth.md`, `-mcp-runner-capture.md`). It has 14 tools in `mcp_gateway/mcp_app.py`. `_run()` reads `project_id_of(get_access_token())` and calls `services/call_guard.guarded_call(project_id=…)`. `_resolve_call` loads the project, and every use case takes `McpCall(project, actor=PersonStepActor.tester())`. Throttles are per project and kind; ANSWER is shared with the runner window.
- **Auth**: `services/authenticate_bearer.py` `BearerVerifier` takes either of two bearers:
  - a `pha_` OAuth access token: one query joining `McpOAuthToken → McpGrant → project` with the access rule from `services/oauth_access.py` (user active, writable membership, project not archived, grant not revoked), then a check of `fingerprint_matches(grant)` against `McpGrant.api_key_fingerprint`;
  - a Connection key, via `projects.services.get_project_by_api_key`.

  `McpAccessToken.project_id` is always one project.
- **OAuth models** (`mcp_gateway/models.py`, migration `0002_oauth`):
  - `McpGrant` has a single `project` FK and `api_key_fingerprint`.
  - `McpAuthorizationCode` has a single `project` FK.
  - Services that use them: `oauth_consent.approve(handle, user, project_id)`, `oauth_exchange_code._exchange_locked` (re-checks `can_approve`, creates the grant), and `oauth_refresh._refresh_locked`, which revokes the whole grant (`membership`) when `live_grants()` or the fingerprint fails.
  - `oauth_connections.list_connections` gives one row per grant with one project name.
- **Consent page**: `/connected-apps/authorize/<handle>/` (`mcp_gateway/views.consent`, `forms.ConsentForm` with a radio `project` field, `templates/mcp_gateway/consent.html` "…on one of your projects").
- **Connected apps page**: `/connected-apps/` (`connections.html`) with Revoke.
- **Resource**: one resource, `canonical_resource()` = `SITE_URL/mcp` (`oauth_policy.py`). Metadata is served at `/.well-known/oauth-protected-resource[/mcp]`, nginx has an anchored regex location for it, and `/token` refuses a foreign `resource` (`oauth_app._token_endpoint`).
- **Earlier decision**: the OAuth plan (`docs/plan/feat-mcp-oauth.md` › Alternatives) rejected "bind tokens to the user only and pick the project per call" so the tools could stay unchanged. This request reverses that trade-off on purpose.

### What the clients support (researched 2026-11-08)

- **Claude custom connectors** (claude.ai, Desktop, mobile, Cowork; same infrastructure):
  - Authentication is OAuth: DCR or CIMD, callback `https://claude.ai/api/mcp/auth_callback`, PKCE S256, refresh on 401.
  - **Request headers** (a static `Authorization: Bearer <key>`) are **beta, for a limited set of organizations**. Only an organization Owner sets them, and they are "the organization's credential, not a person's". The docs say: "Never accept it in the URL … don't read tokens from query parameters".
  - On Team/Enterprise an Owner adds the connector once for the org and each member connects with their own account.
  - The Free plan allows one custom connector.
  - The protected-resource `resource` "must equal the URL as the user enters it, including any path component".
  - Sources: https://claude.com/docs/connectors/custom/remote-mcp, https://claude.com/docs/connectors/building/authentication.md
- **Duplicate URLs**: claude.ai refuses a second connector with the same URL ("A server with this URL already exists" / "A connector with this URL already exists in your organization"). The URL alone is the uniqueness key. The issue is open, labelled bug, and Anthropic has not answered. Users report mixed results with `?query` variants; a different path is a different URL. Source: https://github.com/anthropics/claude-ai-mcp/issues/178
- **Codex**: `[mcp_servers.<name>]` entries are keyed by name, so several entries with the same URL and different `bearer_token_env_var` values already work. That means Codex and Claude Code (`claude mcp add <name> --header …`) can already reach several projects with one Connection key per entry. Codex also supports OAuth (`codex mcp login <name>`). Source: https://developers.openai.com/codex/mcp
- **SDK**: the MCP SDK is `mcp~=2.3.0` (requirements.txt). The resource-server side stays our own `BearerVerifier`.

**Conclusion**: the blocked case is OAuth clients (claude.ai/Cowork/Desktop, ChatGPT). Header clients already have a workaround. Static-header Claude connectors are beta, Owner-only and shared, and still limited to one per URL; they are not the path to fix.

## Goals
- One OAuth connection (one grant) can cover several projects the user can write to. The user chooses them on the consent page and can change them later.
- The tools work on any project the connection covers. The model finds the projects with `list_projects`.
- Per-project revocation: losing write access, archiving the project or regenerating its Connection key ends that project's access for that connection only. Other projects on the same connection keep working.
- Existing single-project grants and Connection key bearers keep their access. Their clients must pass `project_id` (see Decisions › D5: this is a deliberate breaking change of the tool contract).
- The no-trace rule is unchanged.

## Non-goals
- A personal or user-level static MCP key. Header clients already work with one key per entry.
- Per-project resource URLs (`/mcp/p/<id>`), keys in URLs, "all my projects including future ones" grants (D3), CIMD.
- Changing the claim, hand-in or capture rules, or the per-project throttles.
- Superuser implicit access: approval stays membership-only, as today (D6).
- Inferring the project from a single-project credential: `project_id` is always required (D5).

## Design

### Data model (`mcp_gateway/models.py`, migration `0004_grant_projects`)
- New `McpGrantProject`, one row per project a grant covers:
  - `grant` FK (CASCADE, related_name `projects_links`), `project` FK (CASCADE);
  - `api_key_fingerprint` (moved from `McpGrant`: each project has its own key);
  - `created_at`, `revoked_at` (nullable), `revoked_reason` (`OAuthGrantRevokedReason` choices plus a new `REMOVED` = "Removed by you");
  - constraint `unique (grant, project)`, index `(project, revoked_at)` for "every connection of this project".
- `McpAuthorizationCode.project` is replaced by `project_ids` (JSONField list of ints, 1..`MCP_OAUTH_GRANT_MAX_PROJECTS`). Codes live 60 s and every id is re-checked at exchange, so an FK set is not worth it.
- `McpGrant.project` and `McpGrant.api_key_fingerprint` are removed after the data step.
- Migration, in one file:
  1. add the model and the field;
  2. a RunPython that copies every grant's `(project, api_key_fingerprint)` into a link (revoked grants included, so the history still shows the project name);
  3. remove the old fields.

  In-flight authorization codes (≤ 60 s) are deleted: the user retries the sign-in. The table is small (OAuth was added on 2026-11-07, default off), so this is not a populated-table risk. The migration is reversible only for grants with exactly one link; the reverse RunPython raises otherwise.
- New constant `MCP_OAUTH_GRANT_MAX_PROJECTS = 50` in `limits.py`. It bounds the consent form, the code's id list and the links per grant.

### Access rule (`services/oauth_access.py`, extended; still the one definition)
- **Live link**: the link is not revoked, its grant is not revoked, the user is active, the user has a writable membership on the link's project, the project is not archived, and the link's fingerprint equals `api_key_fingerprint(project.api_key)` (compared in Python, constant time).
- New helpers replace `live_grants` / `fingerprint_matches(grant)`:
  - `live_links()`: a queryset of links passing the SQL part, with `select_related("project")`;
  - `link_fingerprint_matches(link)`.
- `writable_projects` and `can_approve` are unchanged. A new `approvable_ids(user_id, ids) -> set[int]` does one query for many ids.

### Services
- **`services/authenticate_bearer.py`**:
  - **OAuth path**: one query on the token hash joined to the grant (not revoked, user active), with an `EXISTS` of a link that passes the SQL part of the rule.
    - It no longer carries a project.
    - `McpAccessToken` gains `grant_id` and loses `project_id` for OAuth. Key tokens keep `key_project_id`.
    - A grant with no live link at all answers 401, so the client re-authenticates, as today when the single project is lost.
    - The fingerprint is checked per call (below), not here.
  - **Key path**: unchanged.
  - `project_id_of` becomes `caller_of(token) -> McpCaller`, a frozen dataclass in `mcp_gateway/oauth_types.py`: `grant_id | None`, `key_project_id | None`, `auth`.
- **`services/call_guard.py`**:
  - `guarded_call(caller=McpCaller, project_id: int | None, …)`. `_resolve_call` resolves the project for this call:
    - `project_id` is always given: the tool schema requires it (D5). `guarded_call` takes `project_id: int`, except for `list_projects`, which calls a separate caller-scoped entry (`guarded_caller_call`, no project resolution, throttle keyed by the caller).
    - **key**: `project_id` must equal `key_project_id`. Any other id gets the same refusal as an unknown project.
    - **OAuth**: one query on `live_links().filter(grant_id=…, project_id=…)`, plus a fingerprint compare.
  - Unknown, removed or dead projects all answer one text, `_PROJECT_REFUSED`: "This connection cannot use project <id>. Call list_projects for the projects you can use." It does not reveal whether a project id exists, and it echoes only the integer the client sent.
  - A **missing** `project_id` never reaches `guarded_call`: the SDK refuses it while validating arguments (`mcp/server/mcpserver/tools/base.py`: pydantic `ValidationError` → `ToolError("Error executing tool <name>: … project_id Field required")`). The SDK owns that text, so the list_projects pointer goes in the `ProjectIdArg` description, every tool description and the server instructions. Do not work around the SDK with an optional parameter plus a forced `required` in the schema.
  - Throttles stay per project. The log line adds nothing new; `project=` is already logged.
  - Query count per call stays at 2, as today (verifier + resolve).
- **New `services/list_projects.py`** (`list_projects` tool), one query:
  - **OAuth**: the grant's live links ordered by name, each with id, name and running manual run (id, name, or null). The running run comes from a `Subquery` on `TestRun` (Manual, STARTED, newest).
  - **Key**: the key's project only (it works for Connection-key clients, so they discover the `project_id` they must pass).
  - Dead links are omitted (a fingerprint mismatch is filtered in Python).
  - No capture block per project. `get_project(project_id)` keeps that, because readiness is a runner round trip and must not be multiplied across projects.
- **`services/oauth_consent.py`**: `approve(handle, user, project_ids: list[int])` checks `approvable_ids` equals the set, then stores `project_ids` on the code. `ConsentView` is unchanged.
- **`services/oauth_exchange_code.py`**: under the code lock, it re-checks `approvable_ids` and creates the grant plus one link per project, each with the fingerprint of that project's key. If none are still approvable, it refuses as today (`invalid_grant`, code spent). If some are, it links only those. Silent narrowing is acceptable here because the 60-second code is about to be exchanged and the Connected apps page shows the result.
- **`services/oauth_refresh.py`**:
  - It persists dead links: `revoked_at=now`, reason `membership` (label "Access to the project ended", covering lost write access, archive and key regeneration). This way a member who loses then regains write access does not silently get the link back. That matches today, where refresh revokes the single-project grant.
  - The grant is revoked (`membership`) only when no live link is left. Otherwise it rotates as today.
- **New `services/oauth_grant_projects.py`** (Connected apps page edit; D2): `set_grant_projects(user, grant_id, project_ids)`:
  - It locks the grant (owned by user, not revoked).
  - Added ids must pass `approvable_ids`. They get new links with fresh fingerprints, or a revoked link is reactivated by clearing `revoked_*` and taking a fresh fingerprint.
  - Removed ids are revoked with `REMOVED`.
  - At least one project must stay; to remove the last, use Revoke.
  - Errors raise a new `GrantProjectsRefused` in `mcp_gateway/exceptions.py`.
- **`services/oauth_connections.py`**: one row per grant with a list of `(project_name, status, status_label)` per link. Grant status: active if any link is live. The query uses `prefetch_related(Prefetch("projects_links", queryset=…select_related("project").annotate(has_write=…)))`: 2 queries in total, no N+1.
- **No change** to the use case modules (`read_*`, `claim_case`, `mark_step`, `hand_in`, `capture_*`, …). They already take `call.project`.

### Tool contract (`mcp_gateway/mcp_app.py`, `schemas.py`, `workflow.py`)
- New `ProjectIdArg = Annotated[int, Field(ge=1, le=_ID_MAX, description="Required. The project_id from list_projects; call list_projects first if you do not have it.")]`. It is the **first, required parameter** of every project-scoped tool, which is all 14 existing tools. There is no default, so the JSON schema lists it in `required`.
- `_run(tool, kind, project_id, pivot_id, use)` passes it to `guarded_call`.
- Request DTOs that are built from tool arguments do not change; `project_id` is consumed by `guarded_call`, not by the use cases.
- **New tool `list_projects`** (READ, `_READ` annotations), the 15th tool. It returns `ListProjectsResponse { projects: [ProjectRef { project_id, name (case_line), manual_run: RunRef | null }] }`. Its READ throttle uses a per-caller key (`mcp:read:grant:<id>` / the key's project) because it is not project-scoped.
- **Always required (D5)**: one contract for every credential, so a client never breaks later when a second project is added to its connection, and every write names its project.
- **Breaking change for connected clients**: every existing client (single-project OAuth grants, Codex or Claude Code entries with a Connection key) must re-read the tool list and pass `project_id`.
  - Hosted clients re-read the list on reconnect; Claude and ChatGPT fetch it per session.
  - `list_projects` is how a Connection-key client learns its `project_id`.
  - The server sends no `tools/list_changed` notification: the gateway is stateless HTTP and holds no sessions.
  - The change note and the Connected apps "How to connect" card say so. Saved prompts or scripts that call tools without `project_id` get the SDK's "Field required" error.
- **Not inferring the project from `pivot_id`**: that would need a lookup before authorization, it hides which project a write goes to, and `list_runs` / KB / `list_windows` have no pivot anyway.
- `WORKFLOW_TEXT` / server instructions:
  - start with `list_projects`; every other tool requires its `project_id`, also when there is only one project;
  - `SERVER_NAME` stays the same; `instructions=WORKFLOW_TEXT` is the updated text, and `get_project`'s `how_to` returns it too;
  - one claimed case at a time **per project** (the server rule is still one claim per run);
  - never mix a case of one project with another project's `project_id` (refused anyway: `load_own_claim` filters by project).
- Tool descriptions: replace "the project's …" with "the chosen project's …" where it reads oddly. `get_project` becomes "Read one project (project_id from list_projects)". `list_projects` gets the "Start here" wording.

### Pages and forms
| URL | Method | Permission | Form | Template / context | Errors |
|---|---|---|---|---|---|
| `/connected-apps/authorize/<handle>/` | GET, POST | `@login_required`; approval needs a writable membership per project (service) | `ConsentForm.projects`: `TypedMultipleChoiceField` (int, checkboxes, required for Allow, max `MCP_OAUTH_GRANT_MAX_PROJECTS`, preselected when there is exactly one) + `decision` | `consent.html`: heading "…wants to act as a tester on your projects", checkbox list, the same context keys with `form.projects` | 400 with an error for none chosen or a refused id (`ConsentProjectRefused`); unavailable or throttled pages as today |
| `/connected-apps/` | GET | `@login_required` (own grants only) | none | `connections.html`: `connections` rows now carry `projects: list[ConnectionProjectRow]` (name, status, status_label); the "How to connect" text adds "one connection can cover several projects" | as today |
| `/connected-apps/<grant_id>/projects/` (new, `mcp_gateway:edit_projects`) | GET, POST | `@login_required`; the grant must be the user's and active (service, 404 otherwise) | `GrantProjectsForm`: the same multiple choice field over `writable_projects(user)`, initial = live links | `mcp_gateway/edit_projects.html` (new): app name, checkbox list, Save | 404 for an unknown or not-owned grant; 400 with a form error for none chosen or a refused project |
| `/connected-apps/<grant_id>/revoke/` | POST | as today | as today | | |

The consent page still shows the trusted client name, the redirect host and the full redirect URI. Projects are rendered with auto-escaping; no `|safe`.

### Background work and WebSockets
- No new Celery task. `sweep_mcp_oauth` also deletes links of grants it deletes (CASCADE), so no change is needed.
- No controller protocol change; CLIENT_VERSION is unchanged. Capture tools resolve the project per call, as before.

### Settings / infrastructure
- No new env var. The resource stays `SITE_URL/mcp`. No nginx or compose change.
- Rollout order: `migrate`, then restart `mcp` and the Django app together. The verifier and consent use the new tables. An old `mcp` process running against the new schema fails OAuth calls for the restart window (seconds). Key bearers are unaffected.

### Data flow
1. In Claude, the user adds the connector `https://<host>/mcp` (unchanged). Claude gets a 401, discovers OAuth, registers, then opens `/authorize`, which 302s to the consent page.
2. The user ticks projects A and B and presses Allow. `approve` stores `project_ids` on a 60 s code, and the browser is redirected to the Claude callback.
3. `/token` → `exchange_code` re-checks each project and creates `McpGrant` plus links A and B, each with its own key fingerprint.
4. A tool call carries the Bearer `pha_…`. `BearerVerifier` loads the grant (≥ 1 live link) → `McpCaller(grant_id)`.
5. `list_projects` returns A and B. `claim_case(project_id=B, pivot_id=…)` → `guarded_call` resolves link B (live rule plus fingerprint) → `McpCall(project=B, tester)` → the unchanged use case.
6. When B's key is regenerated, calls with `project_id=B` are refused at once and A keeps working. At the next refresh, link B is revoked. If A is archived too, there is no live link left: 401, and the refresh revokes the grant.
7. To add project C, the user goes to Connected apps › Edit projects and ticks C, which creates a link with C's current fingerprint. No reconnect in Claude is needed.

## Alternatives considered
- **A. Per-project URL** (`/mcp/p/<id>/`, one connector per project):
  - It works around Claude's same-URL rule because paths differ.
  - It costs a resource per project: path-inserted protected-resource metadata per project, `resource` binding of codes and tokens per path, nginx routing, and `/token` resource checks against a family of URLs.
  - User cost is worse:
    - Free plan: one custom connector, so still one project.
    - Team/Enterprise: an Owner must add one org-wide connector per project, visible to every member.
    - The model sees N copies of 15 tools.
    - Each new project needs a new connector and sign-in.
  - **A′: Connection key in the path** for header-less clients. Rejected outright: Anthropic's docs and the MCP spec forbid credentials in URLs, which end up in nginx access logs, proxies and client configs.
- **B. One grant, many projects** (chosen, D1):
  - One connector and one sign-in. It works on every Claude plan and on ChatGPT. Projects are added or removed without reconnecting.
  - The authorization stays per project, per user and per key.
  - The cost is a `project_id` argument and one new table.
- **C. Personal or user-level MCP key** (a static bearer that unlocks the user's writable projects):
  - It only helps header clients. Those already reach several projects with one Connection key per named entry.
  - On claude.ai, header auth is beta and Owner-only, and the static header is "the organization's credential", which conflicts with a personal key.
  - It adds a new secret surface (creation, rotation, UI, revocation semantics) for no blocked user. Rejected (D4).
- **D. One grant per project, minted together under one token** (consent picks several projects, a hidden grant per project, the token maps to many grants):
  - It is the same tool contract as B, with more rows.
  - Refresh and revoke semantics would be split across grants that one token carries.
  - The link table in B is simpler.
- **E. Grant covers "all writable projects, now and later"**: less friction, but it silently widens the access of a sign-in made earlier. Rejected (D3).

## Risks and mitigations
- **Wider blast radius of one token**: a stolen access or refresh token now reaches several projects.
  - Tokens still expire in 1 h, refresh rotates with reuse detection, and the user picks projects explicitly (least privilege).
  - The cap is `MCP_OAUTH_GRANT_MAX_PROJECTS`. Revoke ends every project of the grant at once.
- **Auth regression on the hot path**: the verifier query changes. Keep it to one query with `EXISTS`. Per-call resolution is one indexed query, so there are 2 queries per call, as today. Check with `CaptureQueriesContext` in a shell smoke test.
- **Cross-project confusion by the model**: a claim token used with the wrong `project_id` is refused (`load_own_claim` filters by project). `project_id` is required everywhere, so nothing is ever guessed.
- **Information leak via project ids**: unknown, dead or foreign ids get one neutral refusal text.
- **Link reactivation through the edit page**: only for the grant owner, POST with CSRF, `approvable_ids` re-checked, and a fresh fingerprint taken. This is equivalent to a new consent by the same signed-in user. The Allow throttle (`MCP_OAUTH_CONSENT_PER_MINUTE`) also applies to the edit POST.
- **Migration**: the data copy on a small table. Codes in flight (≤ 60 s) are dropped, and the user retries the sign-in.
- **No-trace**: unchanged. The actor is still `PersonStepActor.tester()`. Project names appear only on the user's own Connected apps and consent pages (as today), never in run, case or TestRail output.
- **Throttle budget**: per project, so a grant on 10 projects gets 10× the read budget across projects. This is acceptable and the same as 10 connections today.
- **Rollout**: `mcp` and the app must restart together after migrate. This is noted in the deploy skill.
- **Breaking tool contract (D5)**: clients that cache the old schema or hard-code calls fail with "Field required" until they re-read the tools. Mitigations:
  - the change note and the Connected apps card tell users to reconnect, or restart Codex / Claude Code;
  - `list_projects` works for every credential;
  - the refusal texts and descriptions point to it.
  
  Staged rows and claims survive: a claim made before the deploy keeps its token and is reachable with `project_id`.

## Implementation steps

Parts are split by file ownership: no file belongs to two parts. Part 0 is written first by the orchestrator. Parts 1–3 then run in parallel sub-agents. Part 4 is the orchestrator's integration and docs pass.

### Part 0: shared base (first, orchestrator)
Owns:
- `mcp_gateway/models.py`: `McpGrantProject`, `McpAuthorizationCode.project_ids`, `OAuthGrantRevokedReason.REMOVED`, `MEMBERSHIP` relabelled "Access to the project ended", `McpGrant.project` / `api_key_fingerprint` removed;
- `mcp_gateway/migrations/0004_grant_projects.py`: schema, data copy, field removal, codes deleted;
- `mcp_gateway/limits.py`: `MCP_OAUTH_GRANT_MAX_PROJECTS = 50`;
- `mcp_gateway/oauth_types.py`: `McpCaller` (frozen: `grant_id: int | None`, `key_project_id: int | None`, `auth: AuthKind`);
- `mcp_gateway/exceptions.py`: `GrantProjectsRefused`;
- `mcp_gateway/services/oauth_access.py`: `live_links()`, `link_fingerprint_matches(link)`, `approvable_ids(user_id, ids) -> set[int]`; `live_grants` / `fingerprint_matches` removed once the parts stop using them.

Exposes these interfaces to the parts: the names above, plus `McpGrantProject` field names and `related_name="projects_links"`.

### Part 1: OAuth server side (sub-agent)
Owns `mcp_gateway/services/oauth_consent.py`, `oauth_exchange_code.py` and `oauth_refresh.py`.
- `approve(handle, user, project_ids: list[int]) -> str`;
- exchange creates the grant plus its links;
- refresh persists dead links and revokes the grant only when none is left.

Consumes Part 0.

### Part 2: bearer, call guard and tools (sub-agent)
Owns:
- `mcp_gateway/services/authenticate_bearer.py`: verifier with the EXISTS of a live link; `caller_of(token) -> McpCaller | None` replaces `project_id_of`;
- `mcp_gateway/services/call_guard.py`: `guarded_call(caller, project_id: int, …)`, `guarded_caller_call` for `list_projects`, and `_PROJECT_REFUSED`;
- `mcp_gateway/services/list_projects.py` (new);
- `mcp_gateway/schemas.py`: `ProjectIdArg`, `ListProjectsResponse`, `ProjectRef`;
- `mcp_gateway/mcp_app.py`: `project_id` on all 14 handlers, `list_projects` registered (15 tools), descriptions;
- `mcp_gateway/workflow.py`: instructions.

Consumes Part 0. The use case modules under `mcp_gateway/services/` (`read_*`, `claim_case`, `mark_step`, `hand_in`, `release_case`, `add_screenshot`, `stage_shot`, `capture_*`, `list_windows`, `knowledge`, `own_claim`) are not edited. If one must change, it belongs to this part.

### Part 3: pages (sub-agent)
Owns:
- `mcp_gateway/forms.py`: `ConsentForm.projects` as `TypedMultipleChoiceField`, and `GrantProjectsForm`;
- `mcp_gateway/views.py`: consent passes `project_ids`; new `edit_projects` view;
- `mcp_gateway/urls.py`: `<int:grant_id>/projects/` → `edit_projects`;
- `mcp_gateway/services/oauth_connections.py`: rows with `projects: list[ConnectionProjectRow]`, 2 queries;
- `mcp_gateway/services/oauth_grant_projects.py` (new): `set_grant_projects(user, grant_id, project_ids)` and `load_grant_projects(user, grant_id)` for the form's initial;
- `templates/mcp_gateway/consent.html`, `connections.html` (per-project status, Edit projects link, a note that tools need `project_id` from `list_projects`), `edit_projects.html` (new);
- `static/css/punk-hazard.css`, only if a checkbox-card class is missing.

Consumes Part 0, and Part 1's `approve` signature (it is fixed above, so the parts do not wait on each other).

### Part 4: integration and docs (orchestrator, after Parts 1–3)
- Wire the seams: imports of `approve`, `caller_of`, `guarded_caller_call`.
- `grep` that nothing still uses `McpGrant.project`, `project_id_of`, `live_grants` or `fingerprint_matches`.
- Run `pre-commit run --all-files` and `makemigrations --check`.
- Write the docs:
  - `.claude/skills/deploy/SKILL.md`: migrate, then restart `mcp` together with the app;
  - `docs/changed/<date>-mcp-multi-project.md`: includes the breaking `project_id` change and that clients must reconnect or re-read tools;
  - the `CLAUDE.md` MCP gateway paragraph: 15 tools, `list_projects`, required `project_id`, one connection for several projects.

## Verification
- Run `mypy .` and `pre-commit run --all-files`. Run `python manage.py makemigrations --check` after the migration file is written, then `migrate` on a dev database that has an existing single-project grant: it should end with one link carrying the same fingerprint.
- **Tool list** (MCP Inspector): 15 tools; every tool but `list_projects` has `project_id` in `required`; descriptions and instructions point to `list_projects`.
- **Existing grant** (migrated): `list_projects` returns its one project. A call without `project_id` gets the SDK "Field required" error, and the same call with the id works.
- **Connection key** (Codex `bearer_token_env_var`, MCP Inspector):
  - `list_projects` returns the key's project;
  - calls with that `project_id` work;
  - a call without it → "Field required";
  - another project's id → the `_PROJECT_REFUSED` text naming list_projects.
- **New consent with projects A and B** (MCP Inspector OAuth, then claude.ai on staging with `MCP_OAUTH_ENABLED=True`):
  - `list_projects` shows both;
  - a call with a project id not on the grant gets the `_PROJECT_REFUSED` text;
  - claim, mark, capture and hand in on A, then on B: TestRail and the case page show "a tester" for both.
  - The claim token of A used with `project_id=B` is refused.
- **Revocation per project**:
  - Regenerate B's key: B is refused at once and A works. After a refresh, link B is shown as "Access to the project ended".
  - Archive A as well: the next call gets 401, and the refresh revokes the grant.
  - Demote the user to read-only on A, then restore write: after a refresh in between, A stays ended until re-added on Edit projects.
- **Edit projects**:
  - add C: C works without reconnecting;
  - remove A: A is refused;
  - removing all projects is refused with a form error;
  - another user's grant id → 404;
  - a read-only member cannot add a project (refused).
- **Consent page**: zero ticked → error; a tampered id of a project without write access → `ConsentProjectRefused`; Deny still works with nothing ticked.
- **Query counts** (shell, `CaptureQueriesContext`):
  - the verifier: 1 query (plus the throttled `last_used_at` write);
  - `guarded_call` resolve: 1 query;
  - `list_projects`: 1 query;
  - Connected apps: 2 queries for any number of grants and projects.
- Run the `reviewer` and `verifier` agents. The verifier checks the no-trace rule, the per-project revocation, that a project id is never inferred, and the neutral refusal texts.

## Decisions (answered 2026-11-08)
- **D1. Option B**: one connection covers several projects, with `list_projects` and a `project_id` argument. Per-project URLs (option A) are not built.
- **D2. Edit projects**: an Edit projects page on Connected apps adds and removes projects of a live connection. Adding re-checks write access and takes the project's current key fingerprint.
- **D3. No "all projects" option**: explicit ticks only, capped at `MCP_OAUTH_GRANT_MAX_PROJECTS` = 50.
- **D4. No user-level key**: the Connection key stays the only static bearer and covers its one project.
- **D5. `project_id` is always required** on every project-scoped tool (all 14), for every credential: multi-project grants, single-project grants and Connection keys.
  - `list_projects` works for every credential; for a key it returns that one project.
  - Refusals of a wrong project tell the client to call `list_projects`.
  - This is a breaking change for connected clients: they must re-read the tool list and pass `project_id`.
  - The server instructions (`WORKFLOW_TEXT`) and tool descriptions are updated to match.
- **D6. Who may connect a project**: active members with a writable role only; superuser implicit access is excluded, as today.

## Changelog
- 2026-11-08: created (Draft). Re-based on the OAuth gateway (`docs/changed/2026-11-07-mcp-oauth.md`) after it landed mid-planning; parts assuming header-only auth were dropped.
- 2026-11-08: questions answered (Decisions D1–D6); Status Ready. Changed from the draft: `project_id` is required on every project-scoped tool for every credential (was optional for single-project credentials), with a breaking-change note; `list_projects` works for Connection keys; a missing argument is refused by the SDK's validation, a wrong project by `_PROJECT_REFUSED` naming list_projects. Implementation split into Parts 0–4 by file ownership.
- 2026-11-08: implemented (Parts 0–4). Review fixes: migration makes the old fields nullable before the data copy so the reverse works on populated tables; the key fingerprint is now also compared in SQL inside `live_links()` (PostgreSQL built-in `sha256`, not Django's pgcrypto `SHA256`), so the verifier answers 401 once no project is live; refresh locks grant then token (same order as `revoke_grant`); dropped the unused `(project, revoked_at)` index and the duplicate `grant` FK index; removed dead `can_approve` / `McpAccessToken.auth_kind`; `client_names` moved to `oauth_policy`; Edit projects records a dropped project the user can no longer edit as `membership`, not `removed`; consent cap counts de-duplicated ids. `load_grant_projects` returns `GrantProjects` (the page needs the app name).
- Rejected: typing the annotated `has_write` / `run_id` through `WithAnnotations` (the `getattr` / annotation access is local and commented; not worth a TypedDict per query). "Edit projects" for a "No project left" connection: with the SQL fingerprint the next call answers 401 and the refresh revokes the grant, so the state lasts at most one access-token lifetime; reconnecting is the path. Refusing a bad `project_id` before the throttle: unchanged from before, one indexed query, reveals nothing. The `get_project` description mentioning capture: accurate for the tool's existing `capture` block.
- 2026-11-08: second review pass: token lock back to `of=("self",)` (the filter joins the shared client row); Edit projects records `removed` only for a link that was still live (a key-regenerated one keeps `membership`); bearer comments updated for the SQL fingerprint. Third review and verification pass: clean.
