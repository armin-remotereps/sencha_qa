import asyncio
import logging
import signal
import sys
from typing import Final

from vision_worker.client import VisionWorkerClient
from vision_worker.config import ConfigError, load_config, setup_logging
from vision_worker.engine import VisionEngine

logger = logging.getLogger(__name__)

# Exit code for a configuration the worker refuses to start with.
EXIT_CONFIG_ERROR: Final[int] = 2


def main(argv: list[str] | None = None) -> None:
    try:
        config = load_config(argv)
    except ConfigError as exc:
        setup_logging("INFO")
        logger.error("Invalid configuration: %s", exc)
        sys.exit(EXIT_CONFIG_ERROR)
    setup_logging(config.log_level)

    engine = VisionEngine(config.engine)
    client = VisionWorkerClient(config, engine)
    loop = asyncio.new_event_loop()

    def _shutdown_handler() -> None:
        logger.info("Shutdown signal received")
        loop.create_task(client.stop())

    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGINT, _shutdown_handler)
        loop.add_signal_handler(signal.SIGTERM, _shutdown_handler)

    logger.info("Starting vision worker, connecting to %s", config.server_url)
    try:
        # A session the server ended for good (bad token, incompatible
        # version, superseded) also exits 0: it is a deliberate stop, not a
        # crash for a supervisor to restart.
        loop.run_until_complete(client.run())
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
        loop.run_until_complete(client.stop())
    finally:
        loop.close()
        logger.info("Vision worker stopped")


if __name__ == "__main__":
    main()
