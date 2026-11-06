import asyncio
import dataclasses
import logging
import signal
import sys
import time
from typing import Final

from controller_client.client import ControllerClient
from controller_client.config import (
    DEVICE_ENV_NAME,
    PERSON_EMAIL_ENV_NAME,
    RUNNER_ENV_FILE,
    ClientConfig,
    load_config,
    save_env_value,
    setup_logging,
)
from controller_client.desktop import get_desktop_backend
from controller_client.device_monitor import DeviceMonitor
from controller_client.env_check import verify_environment, verify_screen_capture
from controller_client.exceptions import EnvironmentCheckError, ProtocolError
from controller_client.mobile_device import MobileTargetBackend
from controller_client.mobile_discovery import (
    DiscoveredDevice,
    describe_missing_tools,
    discover_devices,
    find_device,
)
from controller_client.mobile_factory import build_mobile_backend
from controller_client.mobile_settings import MobileSettings
from controller_client.person_window import PersonWindow, probe_person_window
from controller_client.protocol import RunnerTarget, normalize_person_email
from controller_client.target import select_mobile_target

logger = logging.getLogger(__name__)

# Tries at the email prompt before it is skipped for this start.
_EMAIL_PROMPT_TRIES: Final[int] = 3
_PLATFORM_NAMES: Final[dict[RunnerTarget, str]] = {
    RunnerTarget.ANDROID: "Android",
    RunnerTarget.IOS: "iOS",
}


# A detached start waits this long for its configured device to be listed.
_DEVICE_WAIT_SECONDS: Final[float] = 60.0
_DEVICE_WAIT_POLL_SECONDS: Final[float] = 5.0


class DeviceSelectionError(Exception):
    """No device to drive; the message says why and how to fix it."""


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


def _platform_name(target: RunnerTarget) -> str:
    return _PLATFORM_NAMES.get(target, target.value)


def _device_label(device: DiscoveredDevice) -> str:
    name = device.name or device.model or "device"
    return f"{_platform_name(device.target)}, {name} ({device.device_id})"


def _describe_device(device: DiscoveredDevice) -> str:
    details = ", ".join(
        part
        for part in (device.model, device.os_version, device.kind.value)
        if part and part != device.name
    )
    return f"{_device_label(device)} [{details}; {device.state.value}]"


def _no_device_message(settings: MobileSettings) -> str:
    platform = _platform_name(settings.target)
    message = f"No {platform} device, emulator or simulator is connected"
    missing = describe_missing_tools(settings.target)
    if missing is not None:
        message += f" ({missing})"
    return (
        f"{message}. Connect one, then start again; check with: python -m "
        "controller_client.diagnose --mobile"
    )


def _ask_device(settings: MobileSettings) -> DiscoveredDevice | None:
    """List the connected devices and ask for one; None when the person quits."""
    platform = _platform_name(settings.target)
    while True:
        devices = discover_devices(settings.target)
        if not devices:
            print(f"\n{_no_device_message(settings)}", flush=True)
            try:
                answer = input("Press Enter to look again, or q to quit: ")
            except EOFError:
                return None
            if answer.strip().lower() == "q":
                return None
            continue
        print(f"\nWhich {platform} device should this Test Runner drive?", flush=True)
        for number, device in enumerate(devices, start=1):
            print(f"  {number}. {_describe_device(device)}", flush=True)
        try:
            answer = input(f"Device number (1-{len(devices)}, Enter to look again): ")
        except EOFError:
            return None
        if answer.strip().isdigit() and 1 <= int(answer) <= len(devices):
            return devices[int(answer) - 1]
        if answer.strip():
            print("Not a device number.", flush=True)


def _save_device(device: DiscoveredDevice) -> None:
    try:
        saved = save_env_value(DEVICE_ENV_NAME, device.device_id, RUNNER_ENV_FILE)
    except (OSError, ValueError) as e:
        logger.warning("Could not save the device to %s: %s", RUNNER_ENV_FILE, e)
        saved = False
    if saved:
        logger.info("Saved the device to %s", RUNNER_ENV_FILE)
    else:
        logger.warning(
            "Using the device for this start only: add %s=%s to %s to keep it",
            DEVICE_ENV_NAME,
            device.device_id,
            RUNNER_ENV_FILE,
        )


def _find_configured_device(
    settings: MobileSettings, wait: bool
) -> DiscoveredDevice | None:
    """CONTROLLER_DEVICE if connected; a detached start (a self-update, a
    service) waits up to _DEVICE_WAIT_SECONDS for it to show up."""
    deadline = time.monotonic() + (_DEVICE_WAIT_SECONDS if wait else 0.0)
    while True:
        device = find_device(settings.target, settings.device_id)
        if device is not None or time.monotonic() >= deadline:
            return device
        logger.info("Waiting for %s=%s", DEVICE_ENV_NAME, settings.device_id)
        time.sleep(_DEVICE_WAIT_POLL_SECONDS)


def _pick_device(settings: MobileSettings, interactive: bool) -> DiscoveredDevice:
    """The device to drive; raises DeviceSelectionError when there is none.

    CONTROLLER_DEVICE names it; when it is empty or not connected an
    interactive start lists the devices and asks (saving the answer), and a
    detached start takes the only connected device.
    """
    if settings.device_id:
        device = _find_configured_device(settings, wait=not interactive)
        if device is not None:
            return device
        if not interactive:
            raise DeviceSelectionError(
                f"{DEVICE_ENV_NAME}={settings.device_id} is not connected. "
                "Connect it (or boot the emulator/simulator) and start again"
            )
        print(f"\n{DEVICE_ENV_NAME}={settings.device_id} is not connected.")
    if interactive:
        chosen = _ask_device(settings)
        if chosen is None:
            raise DeviceSelectionError("No device chosen")
        _save_device(chosen)
        return chosen
    devices = discover_devices(settings.target)
    if not devices:
        raise DeviceSelectionError(_no_device_message(settings))
    if len(devices) > 1:
        ids = ", ".join(device.device_id for device in devices)
        raise DeviceSelectionError(
            f"{len(devices)} devices are connected ({ids}); set {DEVICE_ENV_NAME} "
            f"in {RUNNER_ENV_FILE} to the one to drive"
        )
    return devices[0]


def _with_device(config: ClientConfig) -> MobileTargetBackend | None:
    """Select the phone target; None on the desktop target.

    The backend has no session yet: the device monitor creates it in the
    background, so startup never waits for a first WebDriverAgent build.
    """
    settings = config.mobile
    if settings is None:
        return None
    interactive = config.update_id is None and _is_interactive()
    device = _pick_device(settings, interactive)
    if device.device_id != settings.device_id:
        settings = dataclasses.replace(settings, device_id=device.device_id)
    backend = build_mobile_backend(settings, device)
    select_mobile_target(backend)
    logger.info("Target: %s", _device_label(device))
    return backend


async def _run_client(
    client: ControllerClient, device: MobileTargetBackend | None
) -> None:
    """Run the client, and on a phone target the device monitor beside it."""
    if device is None:
        await client.run()
        return
    monitor = asyncio.create_task(
        DeviceMonitor(device, client.send_device_status).run()
    )
    try:
        await client.run()
    finally:
        monitor.cancel()
        await asyncio.wait({monitor})


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
    return PersonWindow(
        text_size=config.person_window_text_size,
        manual_shortcuts=config.manual_shortcuts,
    )


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

    try:
        device = _with_device(config)
    except DeviceSelectionError as exc:
        logger.error("%s", exc)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
        return
    if device is None:
        logger.info("Desktop backend: %s", get_desktop_backend().name)
        # Not fatal: browser tools keep working without it, and the desktop
        # tools now fail with this same explanation, so the run is still
        # diagnosable.
        try:
            verify_screen_capture()
        except EnvironmentCheckError as exc:
            logger.warning("%s", exc)
    else:
        # The session (and so the first screenshot) is created in the
        # background by the device monitor; its state goes to the server as
        # device_status.
        logger.info(
            "The device session starts in the background; check it with: "
            "python -m controller_client.diagnose --mobile"
        )

    try:
        _serve(config, device)
    finally:
        if device is not None:
            device.stop()


def _serve(config: ClientConfig, device: MobileTargetBackend | None) -> None:
    """Run the client until it stops (shutdown, terminal close or handover)."""
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
        loop.run_until_complete(_run_client(client, device))
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
