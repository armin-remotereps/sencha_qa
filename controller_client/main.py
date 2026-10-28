import asyncio
import logging
import signal
import sys

from controller_client.client import ControllerClient
from controller_client.config import load_config, setup_logging
from controller_client.desktop import get_desktop_backend
from controller_client.env_check import verify_environment, verify_screen_capture
from controller_client.exceptions import EnvironmentCheckError

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> None:
    config = load_config(argv)
    setup_logging(config.log_level, config.effective_log_file)
    if config.update_id is not None:
        logger.info("Started by a Test Runner update (update %s)", config.update_id)

    try:
        verify_environment()
    except EnvironmentCheckError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    logger.info("Desktop backend: %s", get_desktop_backend().name)

    # Not fatal: browser tools keep working without it, and the desktop tools
    # now fail with this same explanation, so the run is still diagnosable.
    try:
        verify_screen_capture()
    except EnvironmentCheckError as exc:
        logger.warning("%s", exc)

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
        # run() returns once it stops reconnecting; release browser, processes
        # and interactive sessions so their threads do not hold up exit.
        loop.run_until_complete(client.stop())
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
        loop.run_until_complete(client.stop())
    except Exception:
        # A runner started by an update may have no terminal: keep the reason
        # in its log file.
        logger.exception("The Test Runner stopped because of an error")
        loop.run_until_complete(client.stop())
        raise
    finally:
        loop.close()
        logger.info("Controller client stopped")


if __name__ == "__main__":
    main()
