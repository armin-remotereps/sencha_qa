import argparse
import logging
import sys
import uuid
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Final

from decouple import config as decouple_config

# Folder the server's cleanup_environment action empties between test cases.
# Overridable so the controller can live in ~/Downloads without wiping itself.
DEFAULT_CLEANUP_DIR: Final[str] = "~/Downloads"
# The Connection key's variable in controller_client/.env.
API_KEY_ENV_NAME: Final[str] = "CONTROLLER_API_KEY"

# File log of a runner started by a self-update (it has no terminal on
# macOS/Linux): <install root>/controller_client/logs/runner.log, 5 MB x 3.
DEFAULT_UPDATE_LOG_FILE: Final[Path] = (
    Path(__file__).resolve().parent / "logs" / "runner.log"
)
_LOG_FILE_MAX_BYTES: Final[int] = 5 * 1024 * 1024
_LOG_FILE_BACKUPS: Final[int] = 3
_LOG_FORMAT: Final[str] = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
# Flags a self-update passes on to the new runner, with their argparse dest.
# Not --api-key: a command line is visible to every local user, so the update
# writes a key given that way into the new install's .env instead.
_CARRIED_FLAGS: Final[tuple[tuple[str, str], ...]] = (
    ("--host", "host"),
    ("--port", "port"),
    ("--reconnect-interval", "reconnect_interval"),
    ("--reconnect-max-interval", "reconnect_max_interval"),
    ("--max-reconnect-attempts", "max_reconnect_attempts"),
    ("--log-level", "log_level"),
    ("--cleanup-dir", "cleanup_dir"),
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
    # Passed by the old runner to the runner a self-update starts.
    parser.add_argument("--update-id", type=_uuid_arg, default=None)
    parser.add_argument("--replaces-pid", type=int, default=None)
    parser.add_argument("--replaces-pid-created", type=float, default=None)
    parser.add_argument("--remove-old-install", type=str, default=None)

    args = parser.parse_args(argv)

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
