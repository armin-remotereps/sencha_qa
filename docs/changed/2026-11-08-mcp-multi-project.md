# One MCP connection for several projects

Date: 2026-11-08
Plan: docs/plan/feat-mcp-multi-project.md

## What changed
- `mcp_gateway/models.py` + migration `0004_grant_projects`: `McpGrantProject` (grant ↔ project link with its own key fingerprint and revocation) replaces `McpGrant.project` / `api_key_fingerprint`; `McpAuthorizationCode.project_ids`; reason `removed`. Existing grants are copied into one link each; in-flight codes are deleted.
- `services/oauth_access.py`: `live_links()` (whole access rule, fingerprint in SQL), `link_fingerprint_matches`, `approvable_ids`.
- Consent page: checkboxes (≤ `MCP_OAUTH_GRANT_MAX_PROJECTS` = 50); `oauth_consent.approve(handle, user, project_ids)`; exchange creates the grant plus links; refresh revokes dead links and the grant only when none is left.
- Connected apps: per-project status; new Edit projects page `mcp_gateway:edit_projects` (`/connected-apps/<grant_id>/projects/`, `services/oauth_grant_projects.py`).
- Tools: new `list_projects` (15 tools); `project_id` is the first required argument of the other 14; `services/call_guard.guarded_call(caller, project_id)` resolves the project per call (`McpCaller` replaces `project_id_of`); instructions in `workflow.py`.

## Why
A claude.ai/ChatGPT connector is one OAuth grant and Claude refuses a second connector with the same URL, so one account could reach only one project.

## Behaviour and contract changes
- **Breaking:** every project tool needs `project_id` (from `list_projects`), for OAuth grants and Connection keys alike. Connected clients must re-read the tool list (reconnect, or restart Codex / Claude Code); calls without it get the SDK's "Field required" error.
- A Connection key still covers only its project; `list_projects` returns it.
- Losing write access, archiving or regenerating the key of one project ends that project only; the connection keeps its other projects.
- Migration `mcp_gateway.0004_grant_projects` (data copy, reversible only while every grant has one link). Restart `mcp` together with the api after migrating.

## Notes for future changes
- Not run against Postgres or a real client yet: run the migration on staging with an existing grant, then walk the plan's Verification list (MCP Inspector, Claude, ChatGPT).
- The SQL fingerprint uses PostgreSQL's built-in `sha256`; Django's `SHA256` would need pgcrypto.
- Lock order is grant → token everywhere (`revoke_grant`, refresh, `set_grant_projects`).
