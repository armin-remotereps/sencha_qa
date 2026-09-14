from __future__ import annotations

import logging
from dataclasses import dataclass

from django.core.cache import cache

from projects.models import Project
from projects.secrets import SecretDecryptionError, decrypt_secret, encrypt_secret
from projects.testrail_client import TestRailClient, TestRailError

logger = logging.getLogger(__name__)

TESTRAIL_PROJECTS_CACHE_TTL = 300
_API_KEY_HINT_LENGTH = 3


class TestRailNotConfiguredError(Exception):
    """Raised when a TestRail operation runs on a project without settings."""


def testrail_projects_cache_key(project_id: int) -> str:
    return f"testrail_projects:{project_id}"


# ============================================================================
# SETTINGS
# ============================================================================


def save_testrail_settings(
    *, project: Project, url: str, email: str, api_key: str | None
) -> Project:
    """Store TestRail connection settings. api_key=None keeps the stored key."""
    project.testrail_url = url.strip().rstrip("/")
    project.testrail_email = email.strip()
    update_fields = ["testrail_url", "testrail_email", "updated_at"]
    if api_key is not None:
        project.testrail_api_key_encrypted = encrypt_secret(api_key)
        update_fields.append("testrail_api_key_encrypted")
    project.save(update_fields=update_fields)
    cache.delete(testrail_projects_cache_key(project.id))
    return project


def clear_testrail_settings(project: Project) -> None:
    project.testrail_url = ""
    project.testrail_email = ""
    project.testrail_api_key_encrypted = ""
    project.save(
        update_fields=[
            "testrail_url",
            "testrail_email",
            "testrail_api_key_encrypted",
            "updated_at",
        ]
    )
    cache.delete(testrail_projects_cache_key(project.id))


def _decrypt_api_key(project: Project) -> str:
    if not project.testrail_api_key_encrypted:
        return ""
    try:
        return decrypt_secret(project.testrail_api_key_encrypted)
    except SecretDecryptionError:
        logger.warning(
            "TestRail API key for project %s cannot be decrypted; treating as unset",
            project.id,
        )
        return ""


def get_testrail_api_key_hint(project: Project) -> str:
    """Last few characters of the stored key, for the settings form hint."""
    return _decrypt_api_key(project)[-_API_KEY_HINT_LENGTH:]


def build_testrail_client(project: Project) -> TestRailClient:
    api_key = _decrypt_api_key(project)
    if not (project.testrail_url and project.testrail_email and api_key):
        raise TestRailNotConfiguredError(
            "TestRail is not configured for this project. Add the URL, email and API key under Settings."
        )
    return TestRailClient(
        url=project.testrail_url, email=project.testrail_email, api_key=api_key
    )


@dataclass(frozen=True)
class TestRailConnectionResult:
    ok: bool
    message: str
    project_count: int = 0


def check_testrail_connection(project: Project) -> TestRailConnectionResult:
    try:
        with build_testrail_client(project) as client:
            projects = client.get_projects()
    except TestRailNotConfiguredError as exc:
        return TestRailConnectionResult(ok=False, message=str(exc))
    except TestRailError as exc:
        return TestRailConnectionResult(ok=False, message=exc.message)
    count = len(projects)
    return TestRailConnectionResult(
        ok=True,
        message=f"Connected. {count} TestRail project{'s' if count != 1 else ''} visible to this account.",
        project_count=count,
    )
