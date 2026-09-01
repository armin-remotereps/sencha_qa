from __future__ import annotations

from controller_client.exceptions import EnvironmentCheckError

_REMEDIATION = (
    "pip install --force-reinstall --no-cache-dir certifi requests urllib3"
)


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
