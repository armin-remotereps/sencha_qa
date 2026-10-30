import asyncio
import dataclasses
import logging
import signal
import sys
from typing import Final

from controller_client.client import ControllerClient
from controller_client.config import (
    PERSON_EMAIL_ENV_NAME,
    RUNNER_ENV_FILE,
    ClientConfig,
    load_config,
    save_env_value,
    setup_logging,
)
from controller_client.desktop import get_desktop_backend
from controller_client.env_check import verify_environment, verify_screen_capture
from controller_client.exceptions import EnvironmentCheckError, ProtocolError
from controller_client.person_window import PersonWindow, probe_person_window
from controller_client.protocol import normalize_person_email

logger = logging.getLogger(__name__)

# Tries at the email prompt before it is skipped for this start.
_EMAIL_PROMPT_TRIES: Final[int] = 3


def _is_interactive() -> bool:
    """Whether a person started this runner in a terminal (never a detached start)."""
    return sys.stdin is not None and sys.stdin.isatty()


def _read_person_email() -> str | None:
    """Ask for the person's email; None when skipped (blank, or no valid answer)."""
    print(
        "\nWho answers Human in the loop steps on this machine? Enter your Punk "
        "Hazard email so the steps you answer in the Test Runner window are "
        "recorded as yours (saved in .env; leave blank to skip).",
        flush=True,
    )
    for _ in range(_EMAIL_PROMPT_TRIES):
        try:
            raw = input("Your Punk Hazard email: ")
        except EOFError:
            return None
        try:
            return normalize_person_email(raw)
        except ProtocolError as e:
            print(f"{e}. Try again, or leave blank to skip.", flush=True)
    return None


def _with_person_email(config: ClientConfig) -> ClientConfig:
    """Ask an interactive start with no email for one, and save it to .env.

    A detached start (self-update handover, a service, no TTY) never asks.
    """
    if (
        config.person_email is not None
        or config.update_id is not None
        or not _is_interactive()
    ):
        return config
    email = _read_person_email()
    if email is None:
        logger.info(
            "No person email set: steps answered in the Test Runner window are "
            "recorded as answered by a person at the Test Runner"
        )
        return config
    try:
        saved = save_env_value(PERSON_EMAIL_ENV_NAME, email, RUNNER_ENV_FILE)
    except OSError as e:
        logger.warning("Could not save the email to %s: %s", RUNNER_ENV_FILE, e)
        saved = False
    if saved:
        logger.info("Saved your email to %s", RUNNER_ENV_FILE)
    else:
        logger.warning(
            "Using your email for this start only: add %s=<your email> to %s to "
            "keep it",
            PERSON_EMAIL_ENV_NAME,
            RUNNER_ENV_FILE,
        )
    return dataclasses.replace(config, person_email=email)


async def _person_window(config: ClientConfig) -> PersonWindow | None:
    """The person step window, or None when disabled or Tk does not work here."""
    if not config.person_window:
        logger.info("Person step window: off (CONTROLLER_PERSON_WINDOW=false)")
        return None
    result = await probe_person_window()
    if not result.ok:
        logger.warning(
            "Person step window: unavailable (%s); Human in the loop steps are "
            "answered in Punk Hazard. Check with: python -m "
            "controller_client.diagnose --window",
            result.detail,
        )
        return None
    logger.info("Person step window: available (%s)", result.detail)
    return PersonWindow()


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

    loop = asyncio.new_event_loop()
    try:
        window = loop.run_until_complete(_person_window(config))
        if config.person_email_problem is not None:
            logger.warning("%s", config.person_email_problem)
        if window is not None:
            config = _with_person_email(config)
    except KeyboardInterrupt:
        loop.close()
        logger.info("Keyboard interrupt received")
        return
    logger.info(
        "Starting controller client, connecting to %s via proxy %s",
        config.ws_url,
        config.proxy_display,
    )

    client = ControllerClient(config, person_window=window)

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
