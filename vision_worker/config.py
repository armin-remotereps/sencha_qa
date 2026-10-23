import argparse
import ipaddress
import logging
import socket
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from decouple import config as decouple_config

DEFAULT_WEIGHTS_DIR: Final[str] = str(
    Path(__file__).resolve().parent / "omniparser" / "weights"
)
DEFAULT_MAX_RESULT_BYTES: Final[int] = 8 * 1024 * 1024
_SECURE_SCHEME: Final[str] = "wss"
_INSECURE_SCHEME: Final[str] = "ws"
_LOCAL_HOSTNAMES: Final[frozenset[str]] = frozenset({"localhost"})
_TRUE_VALUES: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


class ConfigError(ValueError):
    """The worker configuration is unusable; the worker refuses to start."""


class DevicePreference(StrEnum):
    # cuda, then mps, then cpu.
    AUTO = "auto"
    CUDA = "cuda"
    MPS = "mps"
    CPU = "cpu"


@dataclass(frozen=True)
class EngineConfig:
    device: DevicePreference
    weights_dir: str
    box_threshold: float
    iou_threshold: float
    caption_batch_size: int
    # Upper bound for one serialized parse_result message; the annotated
    # image is downscaled until the message fits.
    max_result_bytes: int


@dataclass(frozen=True)
class WorkerConfig:
    server_url: str
    token: str
    # Empty means "<hostname>-<device>", resolved once the device is known.
    worker_name: str
    # Requests allowed to wait behind the one being parsed; one more is BUSY.
    max_queue: int
    reconnect_interval: int
    # Upper bound of the exponential reconnect backoff, in seconds.
    reconnect_max_interval: int
    log_level: str
    engine: EngineConfig


def _env_str(key: str, default: str) -> str:
    return str(decouple_config(key, default=default)).strip()


def _env_int(key: str, default: int) -> int:
    raw = _env_str(key, str(default))
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from e


def _env_float(key: str, default: float) -> float:
    raw = _env_str(key, str(default))
    try:
        return float(raw)
    except ValueError as e:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from e


def _env_bool(key: str, default: bool) -> bool:
    return _env_str(key, "true" if default else "false").lower() in _TRUE_VALUES


def _is_local_host(host: str) -> bool:
    if host.lower() in _LOCAL_HOSTNAMES:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def validate_server_url(url: str, *, allow_insecure: bool) -> str:
    """Require wss:// unless the server is on this machine or the private network.

    Screenshots of the test machine travel over this socket, so plain ws:// to
    a public host is refused unless VISION_ALLOW_INSECURE=true says otherwise.
    """
    if not url:
        raise ConfigError(
            "VISION_SERVER_URL is not set, e.g. "
            "wss://panel.example.com/ws/vision-worker/"
        )
    parts = urlsplit(url)
    if parts.scheme not in (_SECURE_SCHEME, _INSECURE_SCHEME) or not parts.hostname:
        raise ConfigError(
            f"VISION_SERVER_URL must be a wss:// (or ws://) URL with a host, got {url!r}"
        )
    if (
        parts.scheme == _INSECURE_SCHEME
        and not allow_insecure
        and not _is_local_host(parts.hostname)
    ):
        raise ConfigError(
            f"VISION_SERVER_URL uses ws:// to the public host {parts.hostname!r}; "
            "screenshots would travel unencrypted. Use wss://, or set "
            "VISION_ALLOW_INSECURE=true if you really mean it."
        )
    return url


def _device_preference(raw: str) -> DevicePreference:
    try:
        return DevicePreference(raw.lower())
    except ValueError as e:
        choices = ", ".join(d.value for d in DevicePreference)
        raise ConfigError(f"VISION_DEVICE must be one of {choices}, got {raw!r}") from e


def load_engine_config() -> EngineConfig:
    max_result_bytes = _env_int("VISION_MAX_RESULT_BYTES", DEFAULT_MAX_RESULT_BYTES)
    if max_result_bytes <= 0:
        raise ConfigError("VISION_MAX_RESULT_BYTES must be positive")
    return EngineConfig(
        device=_device_preference(_env_str("VISION_DEVICE", DevicePreference.AUTO)),
        weights_dir=_env_str("OMNIPARSER_WEIGHTS_DIR", DEFAULT_WEIGHTS_DIR),
        box_threshold=_env_float("OMNIPARSER_BOX_THRESHOLD", 0.05),
        iou_threshold=_env_float("OMNIPARSER_IOU_THRESHOLD", 0.7),
        caption_batch_size=_env_int("OMNIPARSER_CAPTION_BATCH_SIZE", 64),
        max_result_bytes=max_result_bytes,
    )


def load_config(argv: list[str] | None = None) -> WorkerConfig:
    parser = argparse.ArgumentParser(
        prog="python -m vision_worker",
        description="Punk Hazard vision worker: runs OmniParser for the server.",
    )
    parser.add_argument("--server-url", type=str, default=None)
    parser.add_argument("--name", type=str, default=None)
    parser.add_argument("--log-level", type=str, default=None)
    args = parser.parse_args(argv)

    token = _env_str("VISION_WORKER_TOKEN", "")
    if not token:
        raise ConfigError("VISION_WORKER_TOKEN is not set")
    max_queue = _env_int("VISION_MAX_QUEUE", 1)
    if max_queue < 0:
        raise ConfigError("VISION_MAX_QUEUE must be 0 or more")
    reconnect_interval = _env_int("VISION_RECONNECT_INTERVAL", 5)
    reconnect_max_interval = _env_int("VISION_RECONNECT_MAX_INTERVAL", 60)
    if reconnect_interval <= 0 or reconnect_max_interval < reconnect_interval:
        raise ConfigError(
            "VISION_RECONNECT_INTERVAL must be positive and at most "
            "VISION_RECONNECT_MAX_INTERVAL"
        )

    return WorkerConfig(
        server_url=validate_server_url(
            args.server_url or _env_str("VISION_SERVER_URL", ""),
            allow_insecure=_env_bool("VISION_ALLOW_INSECURE", False),
        ),
        token=token,
        worker_name=args.name or _env_str("VISION_WORKER_NAME", ""),
        max_queue=max_queue,
        reconnect_interval=reconnect_interval,
        reconnect_max_interval=reconnect_max_interval,
        log_level=args.log_level or _env_str("VISION_LOG_LEVEL", "INFO"),
        engine=load_engine_config(),
    )


def default_worker_name(device: str) -> str:
    # One process per GPU on the same host is pinned with CUDA_VISIBLE_DEVICES;
    # without it in the name, the second process would supersede the first.
    visible = _env_str("CUDA_VISIBLE_DEVICES", "").strip().replace(",", "_")
    suffix = f"-gpu{visible}" if visible else ""
    return f"{socket.gethostname()}-{device}{suffix}"


def setup_logging(log_level: str) -> None:
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Importing the OCR stack (paddle, ultralytics) lowers the root logger to
    # WARNING; the worker's own level is pinned here so its logs survive that.
    logging.getLogger("vision_worker").setLevel(numeric_level)
