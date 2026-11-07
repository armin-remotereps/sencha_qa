# MCP gateway: empty-body requests no longer hang

Date: 2026-11-07
Plan: inline (one-line fix)

## What changed
- `auto_tester/mcp_asgi.py` `RequestBodyCap`: the pre-read body is now replayed as exactly one `http.request` message even when empty, then later calls go to the real `receive()`.

## Why
Claude's browser `GET /authorize` hung until nginx's 30 s timeout (504): with an empty body nothing was replayed, so the SDK's `RequestBodyLimitMiddleware` waited on the real `receive()`, whose only body message was already consumed, until the client disconnected.

## Behaviour and contract changes
- none

## Notes for future changes
- Starlette's TestClient did not reproduce this; test ASGI wrappers with a raw `receive` that blocks after the first message.
