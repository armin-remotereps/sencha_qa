"""The owner's Test Runner download: the runner source plus a generated ``.env``.

Replaces ``projects.services._generate_env_content`` and
``generate_controller_client_zip`` (one ZIP per project with the Connection
key). The ``.env`` carries the runner's own token, decrypted from
``TestRunner.token_encrypted`` so a re-download needs no new token (Q7). The
token never goes into a log, a URL or a filename.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.utils.text import slugify

from accounts.models import CustomUser
from projects.runner_bundle import build_runner_archive
from projects.secrets import decrypt_secret
from runners.exceptions import RunnerNotFoundError, RunnerPermissionError
from runners.models import TestRunner
from runners.services.runner_access import can_manage
from runners.types import RunnerId

_FALLBACK_SLUG = "test-runner"


@dataclass(frozen=True)
class RunnerDownload:
    archive: bytes
    # ``test-runner-<slug>.zip``: ASCII only, safe in Content-Disposition.
    filename: str


def _env_value(value: str) -> str:
    """One line of ``.env``: newlines dropped, so a value cannot add a key."""
    return value.replace("\r", " ").replace("\n", " ")


def runner_env_content(runner: TestRunner, token: str) -> str:
    """The runner's ``.env`` (no Connection key, no person email)."""
    return (
        f"CONTROLLER_HOST={settings.CONTROLLER_SERVER_HOST}\n"
        f"CONTROLLER_PORT={settings.CONTROLLER_SERVER_PORT}\n"
        f"CONTROLLER_RUNNER_TOKEN={_env_value(token)}\n"
        # Display only (the person window title); the server uses the token.
        f"CONTROLLER_RUNNER_NAME={_env_value(runner.name)}\n"
        # Empty = the system proxy settings; see controller_client/example.env.
        "CONTROLLER_PROXY=\n"
        "CONTROLLER_RECONNECT_INTERVAL=5\n"
        "CONTROLLER_MAX_RECONNECT_ATTEMPTS=0\n"
        "CONTROLLER_RECONNECT_MAX_INTERVAL=60\n"
        "CONTROLLER_LOG_LEVEL=INFO\n"
        "CONTROLLER_PERSON_WINDOW=true\n"
        # Base text size of the person window (10-28; A-/A+ change it per session).
        "# CONTROLLER_PERSON_WINDOW_TEXT_SIZE=14\n"
        # Disk cap of the Manual run screenshot spool (MB, at least 64).
        "# CONTROLLER_MANUAL_SPOOL_MAX_MB=2048\n"
    )


def runner_zip_filename(runner: TestRunner) -> str:
    slug = slugify(runner.name)[:48].strip("-") or str(runner.pk)
    return f"{_FALLBACK_SLUG}-{slug}.zip"


def build_runner_download(*, actor: CustomUser, runner_id: RunnerId) -> RunnerDownload:
    """The ZIP for one of the actor's non-archived runners.

    Raises ``RunnerNotFoundError`` (missing or archived) or
    ``RunnerPermissionError`` (not the owner).
    """
    runner = TestRunner.objects.filter(pk=runner_id, archived_at__isnull=True).first()
    if runner is None:
        raise RunnerNotFoundError()
    if not can_manage(actor, runner):
        raise RunnerPermissionError()
    token = decrypt_secret(runner.token_encrypted)
    return RunnerDownload(
        archive=build_runner_archive(runner_env_content(runner, token)),
        filename=runner_zip_filename(runner),
    )
