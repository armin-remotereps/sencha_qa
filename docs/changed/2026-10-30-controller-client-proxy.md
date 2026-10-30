# Proxy for the Test Runner connection

Date: 2026-10-30
Plan: docs/plan/feat-controller-client-proxy.md

## What changed
- `controller_client/config.py`: `CONTROLLER_PROXY` (`PROXY_ENV_NAME`) read from `.env`, validated in `load_config` with `websockets.uri.parse_proxy` (bad value or SOCKS without a port: `parser.error`, exit 2); `ClientConfig.proxy` (`repr=False`) and `proxy_display` (scheme://host:port, no credentials).
- `controller_client/client.py`: `websockets.connect(proxy=config.proxy or True)`.
- `controller_client/main.py`: startup line says "via proxy <proxy_display>".
- `controller_client/requirements.txt`: `python-socks~=3.2.0` (websockets needs it for SOCKS).
- `controller_client/example.env` and `projects/services._generate_env_content` (download ZIP `.env`): `CONTROLLER_PROXY=` (empty).
- `CLIENT_VERSION` 0.10.3, so Update Test Runner delivers it.

## Why
Test machines that can only reach the server through a SOCKS or HTTP proxy.

## Behaviour and contract changes
- New `.env` key `CONTROLLER_PROXY`: `socks5h://`, `socks5://`, `socks4a://`, `socks4://`, `http://`, `https://`, optional `user:pass@`. Empty = system proxy settings (`HTTPS_PROXY` etc.), i.e. unchanged. No CLI flag, no protocol or capability change.

## Notes for future changes
- The value may hold credentials: never log it, `str(InvalidProxy)` or the config; use `proxy_display` / `e.msg`. It stays off the command line, and a self-update carries it via the copied `.env`.
- Only the server WebSocket uses it. Browsers under test and pip / `playwright install` during a self-update are not proxied by it. No value forces a direct connection when a system proxy is set.
- Verified with pre-commit (strict mypy for all configs), config checks for valid and invalid values (no secret in output), and an end-to-end echo through local SOCKS5h and HTTP CONNECT proxies with auth. Not exercised against the real server or a self-update.
