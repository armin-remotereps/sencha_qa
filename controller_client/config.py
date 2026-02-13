import argparse
import logging
from dataclasses import dataclass

from decouple import config as decouple_config


@dataclass(frozen=True)
class ClientConfig:
    host: str
    port: int
    api_key: str
    reconnect_interval: int
    max_reconnect_attempts: int
    log_level: str

    @property
    def ws_url(self) -> str:
        scheme = "wss" if self.port == 443 else "ws"
        return f"{scheme}://{self.host}:{self.port}/ws/controller/"


def _env_str(key: str, default: str) -> str:
    return str(decouple_config(key, default=default))


def _env_int(key: str, default: int) -> int:
    return int(str(decouple_config(key, default=str(default))))


def load_config(argv: list[str] | None = None) -> ClientConfig:
    parser = argparse.ArgumentParser(description="Controller Client")
    parser.add_argument("--host", type=str, default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--api-key", type=str, default=None)
    parser.add_argument("--reconnect-interval", type=int, default=None)
    parser.add_argument("--max-reconnect-attempts", type=int, default=None)
    parser.add_argument("--log-level", type=str, default=None)

    args = parser.parse_args(argv)

    return ClientConfig(
        host=args.host or _env_str("CONTROLLER_HOST", "localhost"),
        port=args.port or _env_int("CONTROLLER_PORT", 8000),
        api_key=args.api_key or _env_str("CONTROLLER_API_KEY", ""),
        reconnect_interval=args.reconnect_interval
        or _env_int("CONTROLLER_RECONNECT_INTERVAL", 5),
        max_reconnect_attempts=args.max_reconnect_attempts
        or _env_int("CONTROLLER_MAX_RECONNECT_ATTEMPTS", 10),
        log_level=args.log_level or _env_str("CONTROLLER_LOG_LEVEL", "INFO"),
    )


def setup_logging(log_level: str) -> None:
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
