import asyncio
import logging
import signal
import sys

from controller_client.client import ControllerClient
from controller_client.config import load_config, setup_logging

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> None:
    config = load_config(argv)
    setup_logging(config.log_level)

    logger.info("Starting controller client, connecting to %s", config.ws_url)

    client = ControllerClient(config)
    loop = asyncio.new_event_loop()

    def _shutdown_handler() -> None:
        logger.info("Shutdown signal received")
        loop.create_task(client.stop())

    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGINT, _shutdown_handler)
        loop.add_signal_handler(signal.SIGTERM, _shutdown_handler)

    try:
        loop.run_until_complete(client.run())
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
        loop.run_until_complete(client.stop())
    finally:
        loop.close()
        logger.info("Controller client stopped")


if __name__ == "__main__":
    main()
