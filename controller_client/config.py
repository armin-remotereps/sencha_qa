import argparse
import logging
import os
import stat
import sys
import tempfile
import uuid
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Final

from decouple import config as decouple_config

from controller_client.exceptions import ProtocolError
from controller_client.protocol import normalize_person_email

# Folder the server's cleanup_environment action empties between test cases.
# Overridable so the controller can live in ~/Downloads without wiping itself.
DEFAULT_CLEANUP_DIR: Final[str] = "~/Downloads"
# The Connection key's variable in controller_client/.env.
API_KEY_ENV_NAME: Final[str] = "CONTROLLER_API_KEY"
# The person at this machine (their Punk Hazard email), sent in the handshake
# so steps answered in the Test Runner window are recorded as theirs.
PERSON_EMAIL_ENV_NAME: Final[str] = "CONTROLLER_PERSON_EMAIL"
# The runner's own .env (the one a self-update copies to the new install).
RUNNER_ENV_FILE: Final[Path] = Path(__file__).resolve().parent / ".env"

# File log of a runner started by a self-update (it has no terminal on
# macOS/Linux): <install root>/controller_client/logs/runner.log, 5 MB x 3.
DEFAULT_UPDATE_LOG_FILE: Final[Path] = (
    Path(__file__).resolve().parent / "logs" / "runner.log"
)
_LOG_FILE_MAX_BYTES: Final[int] = 5 * 1024 * 1024
_LOG_FILE_BACKUPS: Final[int] = 3
_LOG_FORMAT: Final[str] = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
# Flags a self-update passes on to the new runner, with their argparse dest.
# Not --api-key or --person-email: a command line is visible to every local
# user, so the update writes values given that way into the new install's .env
# instead.
_CARRIED_FLAGS: Final[tuple[tuple[str, str], ...]] = (
    ("--host", "host"),
    ("--port", "port"),
    ("--reconnect-interval", "reconnect_interval"),
    ("--reconnect-max-interval", "reconnect_max_interval"),
    ("--max-reconnect-attempts", "max_reconnect_attempts"),
    ("--log-level", "log_level"),
    ("--cleanup-dir", "cleanup_dir"),
)
# Value-less flags a self-update passes on, with their argparse dest.
_CARRIED_SWITCHES: Final[tuple[tuple[str, str], ...]] = (
    ("--no-person-window", "no_person_window"),
)


@dataclass(frozen=True)
class ClientConfig:
    host: str
    port: int
    api_key: str
    reconnect_interval: int
    # Upper bound of the exponential reconnect backoff, in seconds.
    reconnect_max_interval: int
    # 0 means retry forever.
    max_reconnect_attempts: int
    log_level: str
    cleanup_dir: str = DEFAULT_CLEANUP_DIR
    # Whether the person step window may be used (CONTROLLER_PERSON_WINDOW,
    # --no-person-window); it is still off when Tk does not work.
    person_window: bool = True
    # Normalized CONTROLLER_PERSON_EMAIL / --person-email; None when unset.
    person_email: str | None = None
    # Why the .env value of CONTROLLER_PERSON_EMAIL was ignored (logged once
    # logging is set up); None when it was valid or unset.
    person_email_problem: str | None = None
    # Whether person_email came from --person-email (a self-update then writes
    # it into the new install's .env).
    person_email_from_cli: bool = False
    # The CLI overrides this process got, re-passed to the runner a self-update
    # starts (update flags excluded).
    cli_overrides: tuple[str, ...] = ()
    # Whether api_key came from --api-key (a self-update then writes it into
    # the new install's .env).
    api_key_from_cli: bool = False
    # Set only on a runner started by a self-update.
    update_id: str | None = None
    replaces_pid: int | None = None
    replaces_pid_created: float | None = None
    remove_old_install: str | None = None
    log_file: str | None = None

    @property
    def effective_log_file(self) -> Path | None:
        if self.log_file:
            return Path(self.log_file).expanduser()
        if self.update_id is not None:
            return DEFAULT_UPDATE_LOG_FILE
        return None

    @property
    def ws_url(self) -> str:
        scheme = "wss" if self.port == 443 else "ws"
        return f"{scheme}://{self.host}:{self.port}/ws/controller/"


def _env_str(key: str, default: str) -> str:
    return str(decouple_config(key, default=default))


def _env_int(key: str, default: int) -> int:
    return int(str(decouple_config(key, default=str(default))))


def _env_bool(key: str, default: bool) -> bool:
    return bool(decouple_config(key, default=default, cast=bool))


def _person_email_arg(value: str) -> str:
    try:
        email = normalize_person_email(value)
    except ProtocolError as e:
        raise argparse.ArgumentTypeError(str(e)) from e
    if email is None:
        raise argparse.ArgumentTypeError("expected an email address")
    return email


def _env_person_email() -> tuple[str | None, str | None]:
    """CONTROLLER_PERSON_EMAIL from .env/the environment, and why it was ignored."""
    raw = _env_str(PERSON_EMAIL_ENV_NAME, "")
    try:
        return normalize_person_email(raw), None
    except ProtocolError as e:
        return None, f"{PERSON_EMAIL_ENV_NAME} is ignored: {e}"


def _uuid_arg(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as e:
        raise argparse.ArgumentTypeError("expected a UUID") from e


def _carried_overrides(args: argparse.Namespace) -> tuple[str, ...]:
    carried: list[str] = []
    for flag, dest in _CARRIED_FLAGS:
        value = getattr(args, dest)
        if value is not None:
            carried.extend((flag, str(value)))
    for flag, dest in _CARRIED_SWITCHES:
        if getattr(args, dest):
            carried.append(flag)
    return tuple(carried)


def load_config(argv: list[str] | None = None) -> ClientConfig:
    parser = argparse.ArgumentParser(description="Controller Client")
    parser.add_argument("--host", type=str, default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--api-key", type=str, default=None)
    parser.add_argument("--reconnect-interval", type=int, default=None)
    parser.add_argument("--reconnect-max-interval", type=int, default=None)
    parser.add_argument("--max-reconnect-attempts", type=int, default=None)
    parser.add_argument("--log-level", type=str, default=None)
    parser.add_argument("--cleanup-dir", type=str, default=None)
    parser.add_argument("--log-file", type=str, default=None)
    parser.add_argument(
        "--no-person-window",
        action="store_true",
        help="never show the window for Human in the loop steps",
    )
    parser.add_argument(
        "--person-email",
        type=_person_email_arg,
        default=None,
        help="your Punk Hazard email, recorded on steps answered in the window",
    )
    # Passed by the old runner to the runner a self-update starts.
    parser.add_argument("--update-id", type=_uuid_arg, default=None)
    parser.add_argument("--replaces-pid", type=int, default=None)
    parser.add_argument("--replaces-pid-created", type=float, default=None)
    parser.add_argument("--remove-old-install", type=str, default=None)

    args = parser.parse_args(argv)
    if args.person_email is not None:
        person_email, person_email_problem = args.person_email, None
    else:
        person_email, person_email_problem = _env_person_email()

    return ClientConfig(
        host=args.host or _env_str("CONTROLLER_HOST", "localhost"),
        port=args.port or _env_int("CONTROLLER_PORT", 8000),
        api_key=args.api_key or _env_str(API_KEY_ENV_NAME, ""),
        reconnect_interval=(
            args.reconnect_interval
            if args.reconnect_interval is not None
            else _env_int("CONTROLLER_RECONNECT_INTERVAL", 5)
        ),
        reconnect_max_interval=(
            args.reconnect_max_interval
            if args.reconnect_max_interval is not None
            else _env_int("CONTROLLER_RECONNECT_MAX_INTERVAL", 60)
        ),
        max_reconnect_attempts=(
            args.max_reconnect_attempts
            if args.max_reconnect_attempts is not None
            else _env_int("CONTROLLER_MAX_RECONNECT_ATTEMPTS", 0)
        ),
        log_level=args.log_level or _env_str("CONTROLLER_LOG_LEVEL", "INFO"),
        cleanup_dir=args.cleanup_dir
        or _env_str("CONTROLLER_CLEANUP_DIR", DEFAULT_CLEANUP_DIR),
        person_window=(
            not args.no_person_window and _env_bool("CONTROLLER_PERSON_WINDOW", True)
        ),
        person_email=person_email,
        person_email_problem=person_email_problem,
        person_email_from_cli=args.person_email is not None,
        cli_overrides=_carried_overrides(args),
        api_key_from_cli=bool(args.api_key),
        update_id=args.update_id,
        replaces_pid=args.replaces_pid,
        replaces_pid_created=args.replaces_pid_created,
        remove_old_install=args.remove_old_install,
        log_file=args.log_file,
    )


def setup_logging(log_level: str, log_file: Path | None = None) -> None:
    """Log to stderr, and also to a rotating ``log_file`` when given.

    With a log file and no terminal (a detached runner on macOS/Linux, whose
    stderr is runner-start.log) stderr is skipped, so lines are not written
    twice; that file then only catches output that bypasses logging.
    """
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    handlers: list[logging.Handler] = []
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_file,
                maxBytes=_LOG_FILE_MAX_BYTES,
                backupCount=_LOG_FILE_BACKUPS,
                encoding="utf-8",
            )
        )
    if log_file is None or (sys.stderr is not None and sys.stderr.isatty()):
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=numeric_level,
        format=_LOG_FORMAT,
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def save_env_value(key: str, value: str, env_file: Path = RUNNER_ENV_FILE) -> bool:
    """Set ``key=value`` in an existing ``.env``; False when there is none.

    Other lines are kept byte for byte (an earlier assignment of ``key`` is
    replaced, a missing one appended), the file mode is kept and the file is
    replaced atomically, so a crash never leaves a half-written ``.env``. A
    missing file is not created: python-decouple would then stop reading the
    ``.env`` it uses today (in a parent folder) for every other setting.
    Raises OSError when it cannot be written.
    """
    if any(c in value for c in "\r\n\x00") or any(c in key for c in "=\r\n\x00"):
        raise ValueError("A .env value cannot contain line breaks")
    if not env_file.is_file():
        return False
    # surrogateescape: bytes that are not UTF-8 survive the round trip as-is.
    content = env_file.read_bytes().decode("utf-8", "surrogateescape")
    lines = content.splitlines(keepends=True)
    assignment = f"{key}={value}"
    replaced = False
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        is_key = (
            not stripped.startswith("#")
            and "=" in stripped
            and stripped.split("=", 1)[0].strip() == key
        )
        if not is_key:
            kept.append(line)
        elif not replaced:
            ending = line[len(line.rstrip("\r\n")) :] or "\n"
            kept.append(assignment + ending)
            replaced = True
        # A later duplicate is dropped: decouple would let it win.
    if not replaced:
        if kept and not kept[-1].endswith(("\n", "\r")):
            kept[-1] += "\n"
        kept.append(assignment + "\n")
    _replace_file(env_file, "".join(kept).encode("utf-8", "surrogateescape"))
    return True


def _replace_file(path: Path, data: bytes) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
