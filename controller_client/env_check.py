from __future__ import annotations

from controller_client.exceptions import EnvironmentCheckError
from controller_client.screen_capture import describe_screen_capture_failure

_REMEDIATION = "pip install --force-reinstall --no-cache-dir certifi requests urllib3"


def verify_environment() -> None:
    """Raise EnvironmentCheckError if the venv's certifi install is broken.

    A recurring failure on client machines: a `pip install` upgrades/downgrades
    certifi across one of the setup scripts' several separate install passes
    and leaves the on-disk package half-overwritten (e.g. `core.py` from one
    version next to an `__init__.py` from another), so `certifi.where` is
    missing. That break otherwise surfaces deep into a test run, the first
    time some tool makes an HTTPS request -- checking here surfaces it
    immediately at startup instead.
    """
    try:
        import certifi

        certifi.where()
    except Exception as exc:
        raise EnvironmentCheckError(
            "certifi is broken in this environment "
            f"({exc!r}). Fix it by running, inside this venv: {_REMEDIATION}"
        ) from exc


def verify_screen_capture() -> None:
    """Raise EnvironmentCheckError if this machine will not let us grab the screen.

    Every desktop tool the agent has -- the screenshot tool and the OmniParser
    element finder alike -- starts by capturing the screen, so a refused
    capture disables all of them at once. Probing here reports it against the
    controller at startup rather than mid-run against whichever tool happened
    to ask first.
    """
    failure = describe_screen_capture_failure()
    if failure is not None:
        raise EnvironmentCheckError(f"Screen capture is unavailable: {failure}")
