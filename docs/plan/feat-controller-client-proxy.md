# feat: Proxy (SOCKS and HTTP) for the Test Runner connection

Status: Done
Created: 2026-10-30
Request: Let the Test Runner (controller client) reach the server through a
proxy configured in its `.env`, e.g. `CONTROLLER_PROXY=socks5://host:1080` or
`CONTROLLER_PROXY=http://host:3128`. The key must be present in every shipped
`.env` with an empty value (not missing).

## Context
- `controller_client/config.py`: `ClientConfig` + `load_config` read `.env` via
  python-decouple helpers (`_env_str`, ...). CLI flags a self-update re-passes
  are in `_CARRIED_FLAGS`; secrets (`--api-key`, `--person-email`) are kept off
  the carried command line because command lines are visible to local users.
- `controller_client/client.py` `_connect_and_listen`: the single
  `websockets.connect(url, ...)` is the only server connection (self-update
  ZIPs arrive over the same socket). `run()` logs `str(e)` of any connect
  exception and retries.
- `controller_client/main.py`: `load_config` runs before logging is set up;
  logs `Starting controller client, connecting to <ws_url>`.
- `.env` sources: `controller_client/example.env` (copied by
  `scripts/setup.sh|.ps1|.bat` when no `.env` exists) and the server's download
  ZIP `.env` from `projects/services.py::_generate_env_content`. Self-update
  copies the old `.env` byte for byte (`update_layout.py`), so the setting
  carries over; the update ZIP (`projects.runner_bundle`) has no `.env`.
- Library (installed in controller venv, Python 3.13): `websockets` 15.0.1
  (`websockets~=15.0`). `connect(proxy=...)`: `True` (default) = system proxy
  (`urllib.request.getproxies`, i.e. `HTTPS_PROXY`/`ALL_PROXY` env, registry/
  macOS settings), a `str` = that proxy, `None` = direct. Schemes `socks5h`,
  `socks5`, `socks4a`, `socks4`, `http`, `https`, validated by
  `websockets.uri.parse_proxy` (raises `InvalidProxy`, whose `__str__` embeds
  the full proxy URL, credentials included). SOCKS needs `python-socks`
  (`websockets.asyncio.client` imports `python_socks` lazily, else raises
  `ImportError("python-socks is required to use a SOCKS proxy")`). HTTP(S)
  proxies use CONNECT with Basic auth from user info; no extra package.
- `python-socks` 3.2.0 (PyPI): the `asyncio` extra only adds `async-timeout`
  for Python < 3.11, so a plain `python-socks~=3.2.0` suffices. Our code never
  imports it (websockets does), so mypy needs no stub.

## Goals
- `CONTROLLER_PROXY` in `.env`: empty = today's behaviour; a URL = connect
  through it (SOCKS4/4a/5/5h, HTTP, HTTPS, optional `user:pass@`).
- Bad value fails at startup with a clear message, never retried forever.
- Proxy credentials never appear in logs, command lines or error output.
- Key present (empty) in `example.env` and the server's download `.env`.

## Non-goals
- Proxying the browsers under test (Playwright), the controller's HTTP calls
  made by test steps, or `pip`/`playwright install` during a self-update
  (they follow the machine's own `HTTPS_PROXY` env as today). Noted only.
- A CLI flag (`--proxy`): not added (credentials on the command line).
- A value that forces a direct connection despite a system proxy. Not asked;
  can be added later (e.g. `CONTROLLER_PROXY=direct`).
- Server changes beyond the download `.env` line. No protocol change.

## Design
### Decision: empty = system/env proxy (today's `proxy=True`)
Existing installs and their `.env` (copied on self-update without the new
line) behave exactly as now; `HTTPS_PROXY`/`ALL_PROXY` users keep working.
An explicit value overrides the system proxy.

### Decision: `.env` only, no CLI flag
Keeps the URL (possibly with credentials) off `ps` output and out of
`_CARRIED_FLAGS`; self-update carries it via the copied `.env` already.

### Config (`controller_client/config.py`)
- `PROXY_ENV_NAME: Final = "CONTROLLER_PROXY"`.
- `ClientConfig.proxy: str | None = None` (None = use system proxy). Field
  must be `repr=False` (frozen dataclass repr could otherwise leak it).
- `load_config`: read `_env_str(PROXY_ENV_NAME, "").strip()`; empty -> None;
  else validate with `websockets.uri.parse_proxy`. On `InvalidProxy` call
  `parser.error(f"{PROXY_ENV_NAME} is not a valid proxy: {e.msg}")` — use
  `e.msg` (reason only), never `str(e)` (contains the URL). Exit code 2,
  before any connect attempt.
- Property `proxy_display: str` for logs: `"system settings"` when None, else
  `scheme://host:port` built from the parsed `Proxy` (no user info).

### Connection (`controller_client/client.py`)
- `websockets.connect(..., proxy=self._config.proxy if not None else True)`.
  Other kwargs unchanged (for an http proxy websockets sends non-`proxy_`
  kwargs to the server leg; ours are all websocket options, fine).
- Connect failures through the proxy go through the existing `run()` retry
  path. Their messages (`python_socks` `ProxyConnectionError`/`ProxyError`,
  websockets `InvalidProxyStatus`/`InvalidProxyMessage`) carry host/port or
  status, not credentials; `InvalidProxy` cannot happen after startup
  validation.

### Startup log (`controller_client/main.py`)
- Extend the "connecting to" line with `via proxy <proxy_display>`.

### Dependencies
- `controller_client/requirements.txt`: `python-socks~=3.2.0` with a comment
  (used by websockets for SOCKS proxies). Setup scripts and the self-update
  install from `requirements.txt`, so nothing else changes.

### `.env` templates
- `controller_client/example.env`: comment + `CONTROLLER_PROXY=`.
- `projects/services.py::_generate_env_content`: add `"CONTROLLER_PROXY=\n"`
  (one line; the download ZIP `.env`).

### Versioning
- No protocol/capability change; the server ignores it. Bump
  `CLIENT_VERSION` to `0.10.3` so Update Test Runner delivers it (it reports
  a newer version). No `ClientCapability`, no `REQUIRED_CLIENT_CAPABILITIES`
  change; old runners simply have no proxy support.

### Pages, Celery, WebSockets, data model
None.

## Alternatives considered
- Empty = direct (`proxy=None`): would silently break machines that rely on
  `HTTPS_PROXY` today. Rejected.
- `--proxy` CLI flag: leaks credentials to local users; rejected.
- Reading `HTTPS_PROXY` ourselves: websockets already does it.

## Risks and mitigations
- Credential leak: validate with `e.msg` only, `repr=False`, redacted display.
  Implementer must not log `config` or the raw value anywhere.
- `socks5` resolves DNS locally, `socks5h` at the proxy: document both in the
  `example.env` comment.
- `wss` through an http proxy: TLS to the server is tunnelled via CONNECT;
  `https://` proxy uses TLS to the proxy (default certs). No extra options.
- Machines with a broken system proxy: unchanged from today.

## Implementation steps
1. Dependency + templates (files: `controller_client/requirements.txt`,
   `controller_client/example.env`, `projects/services.py`).
2. Config field, validation, redacted display (files: `controller_client/config.py`).
3. Pass `proxy` to `websockets.connect`; startup log; version bump (files:
   `controller_client/client.py`, `controller_client/main.py`,
   `controller_client/protocol.py`).

## Verification
- `controller_client/.venv/bin/mypy --config-file controller_client/setup.cfg controller_client/`
  and `pre-commit run --all-files` clean (server mypy for `projects/services.py`).
- Manual, with `pip install -r controller_client/requirements.txt` in the
  controller venv and a local server on a worktree port:
  - `CONTROLLER_PROXY=` -> connects as before; log says "system settings".
  - SOCKS: `ssh -D 1080 localhost -N` (or a `microsocks`), set
    `socks5h://127.0.0.1:1080` -> connects; stop the proxy -> retry loop logs a
    proxy connection error without credentials.
  - HTTP: `mitmproxy`/`tinyproxy` on 3128 with `http://u:p@127.0.0.1:3128`
    -> connects; log shows `http://127.0.0.1:3128` only.
  - `CONTROLLER_PROXY=ftp://u:secret@x:1` -> exits at once with
    "CONTROLLER_PROXY is not a valid proxy: scheme ftp isn't supported", no
    `secret` printed.
  - Download a fresh Test Runner ZIP: its `.env` has `CONTROLLER_PROXY=`.
  - Self-update an install with a proxy set: new runner connects through it.
- Run `reviewer` and `verifier`.

## Open questions
- None blocking. Assumptions: empty = system/env proxy (unchanged
  behaviour); `.env` only, no CLI flag; no "force direct" value.

## Changelog
- 2026-10-30: created
- 2026-10-30: implemented. Review round 1 (reviewer nits, verifier same): a SOCKS URL without a port is now rejected at startup (websockets would use port 80), and the ValueError message reads "invalid port or host". Both agents checked the fix in the working tree; no further findings.
