# fix: Windows Test Runner shell commands, launch_app and prompts

Status: Done
Created: 2026-10-28
Request: On Windows test machines the shell tools (`execute_command`,
`send_command_input`, `wait_for_command`) all fail. `launch_app` fails for most
Start-menu apps. The agent prompts assume bash. Fix the Test Runner so commands
run in a defined Windows shell through the same `InteractiveSession` interface,
with process-tree cleanup. Make `launch_app` launch Start-apps entries correctly
and register PIDs where it can. Make `cwd` handling and the prompts OS-aware.
Decode command output without crashing. Add a Windows mypy pass so Windows-only
code is type-checked. Keep changes minimal and keep old runners working.

## Context

- `controller_client/interactive_session.py`: `InteractiveSession.start` calls
  `pexpect.spawn("/bin/bash", ["-c", cmd])`. On Windows `pexpect/__init__.py`
  only defines `spawn` under `if sys.platform != 'win32'`, which explains the
  live error `module 'pexpect' has no attribute 'spawn'` (confirmed by reading
  the installed pexpect 4.9.0). The `types-pexpect` stub declares `spawn`
  unconditionally, so even a `--platform win32` mypy pass would not catch this.
- `controller_client/executor.py`: `execute_start_interactive_cmd`,
  `execute_send_input`, `execute_wait_for_command` (loops
  `while session.is_alive(): session.read_output()`) and
  `execute_terminate_interactive_cmd` use the session interface. `execute_command`
  and `execute_command_streaming` (the legacy `run_command` path; server side only
  `projects.services.controller_run_command*` and the
  `test_controller_command` management command use it) call `subprocess` with
  `text=True` and no encoding. On Windows that means the ANSI code page with
  `errors="strict"`. A `UnicodeDecodeError` inside `_read_stream` kills that
  reader thread. The child can then block on a full pipe and `process.wait()`
  never returns.
- `controller_client/process_tracker.py`: `register(pid)`; `kill_all` uses
  `taskkill /F /T /PID` on Windows and `killpg` on POSIX. The POSIX branch is
  chosen with `platform.system()`, which mypy cannot narrow, so it causes 5
  errors under `--platform win32`.
- `controller_client/app_discovery.py`: `_discover_windows_start_apps` stores
  the `Get-StartApps` AppID as `AppCandidate.exec_path`. Possible AppIDs are a
  UWP AUMID (`Microsoft.WindowsCalculator_8wekyb3d8bbwe!App`), a known-folder
  path (`{1AC14E77-...}\WindowsPowerShell\v1.0\powershell.exe`) or an absolute
  path. It runs first and dedupes by display name, so it shadows the `.lnk` and
  Program Files `.exe` entries with the same name. `subprocess.run(text=True)`
  decodes PowerShell's output with the ANSI code page, which garbles non-ASCII
  app names.
- `controller_client/app_launcher.py`: on Windows calls
  `os.startfile(candidate.exec_path)` (with a `# type: ignore[attr-defined]`),
  which fails with WinError 2 for known-folder and AUMID AppIDs and registers no
  PID. On macOS it registers the PID of `open`, which exits at once, so macOS
  apps are not killed by cleanup either. Windows is therefore no worse than
  macOS today.
- `AppCandidate` is also built by `app_close_policy.match_score` and consumed
  by `app_checker.py`; both only use `display_name`/`exec_path`.
- `agents/services/tools_controller.py:106`: `effective_command = f"cd {cwd} &&
  {command}"`. On cmd.exe this does not change drive and breaks on spaces.
- `agents/services/tool_registry.py:_handle_execute_command` passes `cwd`;
  `ToolContext` (`agents/types.py`) carries `project_id`, not the OS.
- OS facts: `Project.agent_system_info["os"]` (from runner
  `platform.system()`: `Windows`/`Darwin`/`Linux`),
  `agents.services.prompt_parts.get_os_name`. Capabilities:
  `Project.agent_capabilities`, `projects/runner_capability_services.runner_supports`;
  `RunnerOutdatedError` (an `InfrastructureError`) pauses a case with fault
  `runner_outdated`, as done for `double_click`.
- Prompts: `prompt_parts.build_desktop_tool_examples` (`ls -la`,
  `/home/user/project`), `build_shell_rules` (append ` &`, `sudo password`,
  `cd /path && ...`), `build_environment_context` (Windows section names no
  shell), `tool_definitions.py` `execute_command` description ("sudo
  password"). Callers of `build_tool_guidelines`/`build_shell_rules`:
  `agents/services/agent_loop.py`, `agents/services/sub_agent_prompts.py`,
  `machine_setup/services/setup_task_prompt.py`; each already has
  `system_info`. The setup lead prompt (`machine_setup/services/setup_prompt.py`)
  only delegates via `run_setup_task` and runs no commands itself.
- Typing: `controller_client/setup.cfg` plus the `mypy-controller-client`
  pre-commit hook run with the host platform (macOS). Running
  `mypy --platform win32` today gives 6 errors: 5 in `process_tracker.py`
  (`killpg`, `getpgid`, `SIGKILL`) and 1 unused ignore on `os.startfile`. The
  `--platform linux` pass is clean. No pywin32 is used (Windows code is
  `ctypes`). The local `stubs/` (Quartz, AppKit) resolve on every platform, so
  no new stubs are needed.
- pexpect 4.9 `PopenSpawn` (read from the installed source): it has no
  `isalive`, no `terminate` and no `exitstatus` until `wait()`. It hardcodes
  `creationflags=CREATE_NEW_PROCESS_GROUP`, so the child shares the runner's
  console. Its `read_nonblocking` returns immediately on an empty queue and
  `expect_loop` sleeps only `delayafterread=0.0001`, so every 5 s read window
  busy-spins a CPU core. `codec_errors` defaults to `strict`.

## Root cause

All items below are confirmed by reading the code. Item 2 was also reported
live.

1. Commands: the POSIX-only `pexpect.spawn` plus `/bin/bash` are used on every
   platform.
2. launch_app: `os.startfile` gets an AppID, which is not a file path.
   `shell:AppsFolder\<AppID>` is the shell namespace that resolves both AUMIDs
   and known-folder AppIDs.
3. cwd: the `cd` is built on the server with POSIX syntax.
4. Prompts: the shell text is OS-agnostic but written for bash.
5. Decoding: there is no explicit encoding and `errors` is strict.
6. No PID: `os.startfile` returns nothing.
7. Typing: mypy only checks the host platform's branches.

## Goals

- `execute_command` / `send_command_input` / `wait_for_command` / terminate work
  on Windows through the unchanged `InteractiveSession` public interface.
- `launch_app` launches Start-apps entries (AUMID and known-folder AppIDs), keeps
  `.lnk`/`.exe` working, and registers a PID when it starts a real `.exe` itself.
- `cwd` works on Windows (drive change, spaces).
- The agent is told which shell runs its commands, with OS-appropriate examples.
- Command output never crashes decoding on any OS.
- pre-commit type-checks the controller for win32 as well as the host.

## Non-goals

- PowerShell as the command shell. The agent can still call
  `powershell -NoProfile -Command "..."` from cmd.
- Killing grandchildren that outlive their `cmd.exe` (for example `start ""
  app`). Windows has no process groups; that would need Job objects.
- PIDs for `.lnk` and `shell:AppsFolder` launches. `ShellExecute` gives no
  handle for them; `close_app` is the agent-side tool for those apps.
- Changing the POSIX `cd {cwd} && ...` behaviour. It stays unquoted so `~/x`
  keeps expanding.
- A Linux mypy pass (clean today; can be added the same way later).
- Reordering Windows discovery (Get-StartApps first). Once Start-apps entries
  launch, the shadowing is harmless.

## Design

### Shell choice: cmd.exe

Commands run as `cmd.exe /d /s /c "<command>"`. The reasons:

- `cmd /c` returns the real exit code of the command. `powershell -Command`
  collapses failures to 0/1 unless the agent writes `exit $LASTEXITCODE`.
- It is the same shell `subprocess(shell=True)` uses, so the legacy
  `run_command` path matches.
- It starts in milliseconds. Windows PowerShell 5.1 takes 0.3 to 1 s per call.
- Piped stdin works for `set /p`, `choice` and console installers.
- `/d` skips AutoRun. `/s` strips exactly the outer quotes, so the command
  string passes through untouched.

The command line is passed to `Popen` as a string. `list2cmdline` would
backslash-escape inner quotes, which cmd does not understand.

### Controller: interactive session (no wire change)

- `controller_client/interactive_session.py`: `InteractiveSession` and
  `InteractiveSessionManager` keep their public API. `InteractiveSession`
  delegates to a platform process object chosen at import time with
  `if sys.platform == "win32": ... else: ...` (the `app_closer.py` pattern, so
  mypy narrows each branch).
- `controller_client/interactive_process_posix.py` (new): today's pexpect code
  moved out unchanged (`/bin/bash -c`, PTY).
- `controller_client/interactive_process_windows.py` (new): plain `subprocess`.
  It does not use `PopenSpawn`, because of its busy loop, the console it shares
  with the runner, and its missing `isalive`/`terminate`.
  - Spawn: `subprocess.Popen` with the cmd line above, `stdin=PIPE`,
    `stdout=PIPE`, `stderr=STDOUT`, `bufsize=0`,
    `creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP`. The child gets
    its own hidden console. A program that reads the console directly, or a
    `chcp`, then never touches the runner's terminal, and Ctrl events do not
    reach the runner. Register `proc.pid` with `ProcessTracker`.
  - Reader: one daemon thread reads raw bytes (`os.read` on the stdout fd,
    4 KiB) into a `queue.Queue`, with `None` as the EOF sentinel.
  - `read(timeout)` keeps the current pexpect semantics: it collects until EOF
    or until `timeout` elapses, using blocking `queue.get(timeout=remaining)`,
    so there is no busy loop. It decodes on the consumer side with an
    incremental decoder: `codecs.getincrementaldecoder("oem")(errors="replace")`.
    A hidden console created by `CREATE_NO_WINDOW` uses the system OEM code
    page, which is what cmd built-ins write to a pipe.
  - `send_line(text)`: write `text + "\r\n"` encoded with `oem`/`replace`, then
    flush. `BrokenPipeError` or `OSError` becomes `ExecutionError` ("process
    has exited").
  - `is_alive()` is `proc.poll() is None`; `exit_code()` is `proc.returncode`.
  - `terminate()`: if alive, kill the tree with the shared
    `process_tracker.kill_process_tree(pid)` (taskkill `/F /T`), then
    `proc.wait(timeout=5)`, then close stdin.
  - Known differences from POSIX: no PTY, so input is not echoed. Programs that
    read only the console (for example `getpass`) cannot be answered and run
    until timeout or terminate. A GUI program started by the command keeps the
    pipe open after `cmd.exe` exits. The read then just waits out its timeout,
    `is_alive` is False, and the tool returns, which is acceptable.

### Controller: process tracker

- Replace the `platform.system()` switch with `sys.platform == "win32"`
  branches. This fixes the 5 win32 mypy errors.
- Add `kill_process_tree(pid) -> bool` (the body of today's `_kill_all_windows`
  per PID) for the Windows session `terminate`.
- PID-reuse guard. The `taskkill /T` risk on Windows becomes real once cmd.exe
  and app PIDs are registered and cleanup runs hours later. `register` records
  `psutil.Process(pid).create_time()`. If the process is already gone, it is
  not registered. `kill_all` skips any PID whose current create time differs
  (or which no longer exists). psutil is already a controller dependency.

### Controller: launch_app on Windows

- `app_discovery.py`: add `AppLaunchKind(StrEnum)` with `PATH` (default: macOS
  `.app`, Linux exec line, Windows `.lnk`), `WINDOWS_EXE` (absolute `.exe`
  path), and `WINDOWS_APP_ID` (Get-StartApps AppID that is not an absolute
  existing path). `AppCandidate` gets `kind: AppLaunchKind =
  AppLaunchKind.PATH` as its last field, so existing constructors are
  unchanged. `exec_path` keeps the AppID for scoring.
  - Get-StartApps entries: an AppID that is an absolute path to an existing
    `.exe` becomes `WINDOWS_EXE`; everything else becomes `WINDOWS_APP_ID`.
  - The Program Files scan produces `WINDOWS_EXE`; `.lnk` scans stay `PATH`.
  - Get-StartApps output decoding: the PowerShell script becomes
    `[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-StartApps |
    ConvertTo-Json -Compress`. It runs with
    `creationflags=CREATE_NO_WINDOW` (the setter calls `SetConsoleOutputCP`,
    and without its own console it would change the runner terminal's code
    page) and `encoding="utf-8", errors="replace"`. Guard the
    `creationflags` with `sys.platform == "win32"`.
- `app_launcher.py`: under a `sys.platform == "win32"` branch (no type ignore
  left):
  - `WINDOWS_APP_ID`: `os.startfile("shell:AppsFolder\\" + app_id)`. No PID.
  - `WINDOWS_EXE`: `subprocess.Popen([path], cwd=<exe dir>,
    creationflags=CREATE_NEW_CONSOLE)` with no std handle redirection (the same
    as a double-click: a console app gets its own window) and register the PID.
    On `OSError` with `winerror == 740` (elevation required), fall back to
    `os.startfile(path)` so UAC shows, with no PID.
  - `PATH` (`.lnk`): `os.startfile(path)`, as today.
- `app_checker.py`, `app_close_policy.py`: no change, because the default kind
  is used.

### Controller: output decoding

- New `controller_client/shell_encoding.py` with
  `SHELL_OUTPUT_ENCODING: Final[str]`: `"oem"` on win32 and `"utf-8"`
  elsewhere. The interactive Windows process and `executor.py` both use it.
- `executor.execute_command` and `execute_command_streaming`: replace bare
  `text=True` with `encoding=SHELL_OUTPUT_ENCODING, errors="replace"`, keeping
  text mode and `bufsize=1`. The streaming reader can no longer die on a
  decode error.

### Controller: protocol

- No payload change. Add optional capability `ClientCapability.WINDOWS_SHELL_V1`
  ("interactive commands run in cmd.exe on Windows"). New runners advertise it
  on every OS (as with the other optional capabilities); only Windows checks
  it. Bump `CLIENT_VERSION` to `0.6.2`; this is a behaviour change, not a wire
  change. `REQUIRED_CLIENT_CAPABILITIES` is unchanged, so old runners still
  connect.

### Server: cwd and runner gate

- `projects/runner_capability_services.py`: add `runner_os_name(project_id) ->
  str`, one `values_list("agent_system_info")` read that returns the `os` value
  or `""`. Putting it in this module keeps all runner-fact reads in one place.
- New `agents/services/command_cwd.py`:
  `with_working_directory(command: str, cwd: str, os_name: str) -> str`.
  - Empty `cwd` returns `command`.
  - Windows: `cd /d "<cwd>" && <command>`. A `cwd` containing `"` raises
    `InvalidWorkingDirectoryError` (new, `agents/exceptions.py`); `"` cannot
    appear in a Windows path. `%VAR%` still expands inside the quotes, which is
    intended.
  - Otherwise: unchanged `cd {cwd} && {command}`.
- `tools_controller.execute_command`, inside `_do` so errors go through
  `safe_tool_call`:
  1. `os_name = runner_os_name(project_id)`.
  2. If `os_name == "Windows"` and `not runner_supports(project_id,
     WINDOWS_SHELL_V1)`, raise `RunnerOutdatedError("This Test Runner cannot
     run shell commands on Windows. Download a fresh Test Runner from the
     Environment page.")`. The case pauses with `runner_outdated` instead of
     the agent burning iterations on a pexpect error.
  3. Build the command with `with_working_directory`.
  `InvalidWorkingDirectoryError` becomes a normal tool error.
- Why the cd is built on the server and not by passing `cwd` to the runner
  (`StartInteractiveCmdPayload.cwd`): old runners ignore unknown payload
  fields. Runner-side cwd would need a capability and a second server-side
  fallback path that builds the cd anyway. The server already knows the OS, and
  an extra PK read is cheap next to a runner round trip.

### Server: prompts

- `prompt_parts.build_shell_rules(*, system_info)`: OS-aware.
  - Shared lines stay: background shell, stateless shell, interactive input.
  - POSIX: "Commands run in `/bin/bash -c`", plus today's ` &` and sudo lines.
  - Windows:
    - Commands run in `cmd.exe /c`, not PowerShell or bash.
    - For PowerShell, use `powershell -NoProfile -Command "..."`.
    - Use the `cwd` parameter, or chain with `cd /d "C:\path" && ...`.
    - Start a GUI app with `launch_app` or `start "" "C:\path\app.exe"`.
    - Start a long-running console process with `start "" /b ...`.
    - Never append ` &`.
    - There is no sudo. Commands run as the Test Runner's user. A UAC prompt
      cannot be answered from the hidden shell; check admin rights with
      `net session`.
- `build_tool_guidelines(*, system_info)` passes it through. Update the
  callers: `agent_loop.py`, `sub_agent_prompts.py`, `setup_task_prompt.py`.
- `build_desktop_tool_examples()`: make the shell examples OS-neutral
  (`execute_command(command="git --version")`,
  `cwd="<the project directory>"`) so the signature does not change.
- `build_environment_context` Windows section: add one line, "Shell commands
  (execute_command) run in cmd.exe." Add the matching bash line to the macOS
  and Linux sections.
- `tool_definitions.py` `execute_command`: replace "sudo password" with "a
  password"; command description: "The command line, in the shell named in
  ENVIRONMENT (cmd.exe on Windows, bash elsewhere)."

### Tooling: Windows mypy pass

- `.pre-commit-config.yaml`: add hook `mypy-controller-client-win32`, the same
  entry as `mypy-controller-client` plus `--platform win32 --cache-dir
  .mypy_cache/controller_client_win32`. The separate cache stops the two
  passes from invalidating each other. No pywin32 types or new stubs are
  needed.
- The Windows code must use `sys.platform` guards, not `platform.system()`, so
  mypy narrows it. Expected leftovers once this plan's code is in:
  `process_tracker.py` (fixed above) and the `app_launcher` ignore (removed
  above).

### Data model / pages / settings

None. No migration, page, form, setting or `example.env` change.

### Data flow (Windows `execute_command` with cwd)

agent tool call → `tool_registry._handle_execute_command` →
`tools_controller.execute_command` → `runner_os_name` / `runner_supports` (1 to
2 PK reads) → `with_working_directory` → `controller_start_interactive_command`
→ channel layer → runner `execute_start_interactive_cmd` →
`InteractiveSession.start` → `WindowsShellProcess` spawns `cmd.exe /d /s /c
"cd /d "C:\x y" && npm install"` in a hidden console and registers the PID →
the read collects output for 5 s or until EOF → `InteractiveOutputPayload`.

## Alternatives considered

- **pexpect `PopenSpawn`.** It busy-spins (`delayafterread=0.0001`) for every
  read window. It cannot set `CREATE_NO_WINDOW`, so the child shares and can
  read from or re-code-page the runner's console. It has no
  `isalive`/`terminate`, so those need `proc` anyway. A plain `Popen` plus a
  reader thread is about the same amount of code and has none of these
  problems.
- **PowerShell as the shell**: rejected; see "Shell choice".
- **Runner-side cwd** (payload field plus capability): it is cleaner on the
  runner but needs a capability-gated dual path on the server. Rejected for
  simplicity.
- **`explorer.exe shell:AppsFolder\...`**: it works too, but spawns an explorer
  process that hands off to the shell and exits with code 1. `os.startfile`
  uses `ShellExecuteW` directly. Keep explorer as the fallback if manual checks
  show `os.startfile` refusing the `shell:` URI.
- **Resolving known-folder AppIDs to real `.exe` paths** (`SHGetKnownFolderPath`
  via ctypes) to get PIDs for most desktop apps: this is more ctypes code for
  a cleanup nicety that macOS does not have either. See Q1.

## Risks and mitigations

- **`taskkill /T` on a reused PID kills an unrelated tree**: mitigated by the
  create-time guard in `ProcessTracker`.
- **OEM decoding is wrong for programs that write UTF-8** (python, git, node):
  `errors="replace"` keeps output flowing. Only non-ASCII characters can
  degrade. This is accepted.
- **`os.startfile("shell:AppsFolder\\...")` is not documented API**: it is
  widely used, and there is a manual check plus the explorer fallback noted
  above.
- **Behaviour change on Windows for Program Files `.exe` launches**:
  `CreateProcess` replaces `ShellExecute`. Mitigated by the elevation fallback
  (740) and by setting `cwd` to the exe directory.
- **Old Windows runners now get a `runner_outdated` pause on the first
  command**, instead of a tool error. This is intended. In machine-setup
  threads a `RunnerOutdatedError` ends the turn the way other infrastructure
  faults do; verify this manually.
- **Leftover processes started with `start ""` survive cleanup**: documented as
  a non-goal. `close_app` and teardown cover GUI apps.
- **Prompt change affects every OS**: the POSIX shell text is kept word for
  word apart from naming bash, which keeps that regression risk low.

## Implementation steps

1. Process tracker: use `sys.platform` branches, add the public
   `kill_process_tree`, and add the create-time guard. (files:
   `controller_client/process_tracker.py`)
2. Shell encoding constant, and explicit encoding with `errors="replace"` in
   the legacy command paths. (files: `controller_client/shell_encoding.py`
   (new), `controller_client/executor.py`)
3. Interactive session split: move the pexpect code to the POSIX module and
   add the Windows `subprocess` process; `InteractiveSession` delegates by
   `sys.platform`. (files: `controller_client/interactive_session.py`,
   `controller_client/interactive_process_posix.py` (new),
   `controller_client/interactive_process_windows.py` (new))
4. App launch kinds, Get-StartApps UTF-8 in a hidden console, the
   `shell:AppsFolder` launch, and `.exe` Popen with PID and the 740 fallback.
   (files: `controller_client/app_discovery.py`,
   `controller_client/app_launcher.py`)
5. Protocol: `WINDOWS_SHELL_V1` and `CLIENT_VERSION` 0.6.2, with comments.
   (files: `controller_client/protocol.py`)
6. Server cwd and gate: `runner_os_name`, `with_working_directory`,
   `InvalidWorkingDirectoryError`, and the `execute_command` wiring. (files:
   `projects/runner_capability_services.py`, `agents/services/command_cwd.py`
   (new), `agents/exceptions.py`, `agents/services/tools_controller.py`)
7. Prompts: OS-aware shell rules and environment line, neutral examples, and
   the tool description. (files: `agents/services/prompt_parts.py`,
   `agents/services/tool_definitions.py`, `agents/services/agent_loop.py`,
   `agents/services/sub_agent_prompts.py`,
   `machine_setup/services/setup_task_prompt.py`)
8. Windows mypy hook, then run `pre-commit run --all-files`. (files:
   `.pre-commit-config.yaml`)

Steps 1 to 5 (controller), 6 (server) and 7 (prompts) touch disjoint files and
can go to parallel sub-agents. Step 3 consumes `kill_process_tree` from step 1
and `SHELL_OUTPUT_ENCODING` from step 2, so do those first or give the
signatures up front. Step 6 consumes `WINDOWS_SHELL_V1` from step 5, so write
step 5 first.

## Verification

- `pre-commit run --all-files` is clean, including the new win32 hook.
  `controller_client/.venv/bin/mypy --config-file controller_client/setup.cfg
  --platform linux controller_client/` is still clean.
- On a Windows test machine with a fresh runner:
  - `execute_command("echo héllo & ver")`: the output is readable and the exit
    code is 0.
  - `execute_command("exit /b 3")` gives exit code 3.
  - `execute_command("dir", cwd="D:\\Some Dir")` (another drive, with a space)
    lists that directory.
  - `execute_command('set /p X=Name? & echo got %X%')`, then
    `send_command_input("abc")`, prints `got abc`.
  - A long `ping -n 30 127.0.0.1` followed by `wait_for_command` returns all the
    output. Terminating mid-run kills `ping` (check Task Manager).
  - CPU stays idle while a session waits (no busy loop).
  - The runner terminal's code page is unchanged after commands and app
    discovery (`chcp` before and after).
  - `launch_app("Windows PowerShell")` (known-folder AppID),
    `launch_app("Calculator")` (UWP AUMID), an app found only via `.lnk`, and a
    Program Files `.exe` all launch. After cleanup, the `.exe`-launched app is
    killed and the others remain (expected).
  - An elevated-manifest `.exe` shows UAC instead of failing.
  - An app with a non-ASCII name is found by `launch_app`.
- With an old (0.6.1) runner on Windows, the first `execute_command` pauses the
  case with fault Runner outdated. On macOS/Linux the old runner still works,
  and `cwd` behaves exactly as before.
- Check the prompt: render a Windows and a Linux case system prompt (for
  example via `manage.py shell -c` calling `build_tool_guidelines`) and confirm
  that the shell lines differ correctly.
- Run the `reviewer` and `verifier` agents.

## Open questions

- Q1. For `launch_app`, should known-folder Get-StartApps AppIDs (for example
  `{6D809377-...}\App\app.exe`) be resolved to real paths with
  `SHGetKnownFolderPath`, so most desktop apps get a tracked PID and are killed
  at cleanup? This costs about 25 lines of ctypes. Without it, those apps are
  launched through `shell:AppsFolder` and only `close_app` or teardown closes
  them, which is the same as macOS today. (Assumption in this draft: no; only
  absolute-path `.exe` candidates get a PID.)
- Q2. Should old Windows runners be gated with `WINDOWS_SHELL_V1`, which
  pauses the case as Runner outdated, or should the plain tool error be left
  as is? (Assumption in this draft: gate it, because the pause tells the
  person exactly what to do.)

## Changelog

- 2026-10-28: created
- 2026-10-28: Q1 answered no (no known-folder resolution), Q2 answered yes
  (gate with `WINDOWS_SHELL_V1`).
- 2026-10-28: review round 1 fixes:
  - `command_cwd.with_working_directory` became `shell_command.build_shell_command`
    (`InvalidShellCommandError`): on Windows it also rejects multi-line commands,
    because `cmd /c` drops everything after the first line. The Windows shell
    rules now say "single line; chain with &&".
  - Windows `ShellProcess`: runs System32 `cmd.exe` by full path. After
    `terminate()` it keeps draining a pipe that a `start /b` grandchild still
    holds, but discards the output. At EOF it waits for cmd's exit, so
    `wait_for_command` does not spin.
  - `kill_process_tree` is now defined only on win32. `interactive_session` gets
    a `Protocol` for the shared `ShellProcess` shape.
  - Get-StartApps is decoded as `utf-8-sig` (a PowerShell 5.1 BOM would break
    `json.loads`).
  - The POSIX pexpect spawn gained `codec_errors="replace"`, a deliberate
    departure from "moved unchanged" (item 5).
  - Rejected: merging `runner_os_name` and `runner_supports` into one read. It
    saves one primary-key read next to a runner round trip of 5 s or more, and
    would mix the generic capability helper with an OS-specific one.
  - Accepted as is: the legacy `run_command` path decodes with `oem`, although
    the runner's own console may use a different code page. `errors="replace"`
    keeps it from crashing, and only non-ASCII output can degrade.
- 2026-10-28: review rounds 2-4: the Windows reader thread itself stops queueing
  output `_EXIT_DRAIN_SECONDS` (5 s) after cmd exits. It keeps draining the pipe,
  so a `start /b` child never blocks. Two read-based triggers were tried first
  and rejected: one could drop cmd's last bytes, and the other never fired in
  the usual `start /b` flow. After EOF, `read()` waits on cmd's exit (at least
  0.1 s), so `wait_for_command` never spins.
