"""Artifacts profile, artifacts inventory and stage-0 (S0) requirements.

Plan: docs/plan/feat-machine-setup-intake.md › Data model.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from projects.models import Project

# Limits shared by the models, the pure rule modules and the forms.
BASE_DIR_MAX_LENGTH = 500
PROFILE_NOTES_MAX_LENGTH = 4000
PROFILE_CHANGE_SUMMARY_MAX_LENGTH = 300
PROFILE_MAX_ITEMS = 60
# Snapshots kept in ArtifactProfileVersion per profile.
PROFILE_MAX_VERSIONS = 20
KEY_MAX_LENGTH = 64
ITEM_NAME_MAX_LENGTH = 200
ITEM_PATH_PATTERN_MAX_LENGTH = 300
ITEM_EXPECTED_VERSION_MAX_LENGTH = 100
ITEM_SOURCE_MAX_LENGTH = 300
INSTALL_HINT_MAX_LENGTH = 600
HOST_MAX_LENGTH = 255
INVENTORY_ERROR_MAX_LENGTH = 500
REQUIREMENT_TITLE_MAX_LENGTH = 200
REQUIREMENT_CHECK_TARGET_MAX_LENGTH = 300
REQUIREMENT_STATUS_DETAIL_MAX_LENGTH = 500
REQUIREMENT_MAX_CONFLICTS = 10
# Requirements per project, across every analysis.
PROJECT_MAX_REQUIREMENTS = 300
ANALYSIS_UNCLEAR_MAX_LENGTH = 500
# Written in ``ArtifactItem.expected_version`` to mean "the newest one found".
LATEST_VERSION = "latest"


class ProfileWriteSource(models.TextChoices):
    USER = "user", "Project member"
    SETUP_AGENT = "setup_agent", "Machine setup agent"
    RESTORE = "restore", "Restore"


class ArtifactKind(models.TextChoices):
    APPLICATION = "application", "Application"
    BUILD = "build", "Build"
    INSTALLER = "installer", "Installer"
    PACKAGE = "package", "Package"
    CONFIG = "config", "Configuration"
    DATA = "data", "Data"
    LICENCE = "licence", "Licence"
    OTHER = "other", "Other"


class ArtifactOs(models.TextChoices):
    """The test machine OS a base directory or an inventory belongs to.

    ``profile_rules.artifact_os_for`` maps the runner's system info name
    ("Windows" / "Darwin" / "Linux") to one of these.
    """

    WINDOWS = "windows", "Windows"
    MACOS = "macos", "macOS"
    LINUX = "linux", "Linux"


class InventorySource(models.TextChoices):
    # The runner's own ``scan_directory`` action (ARTIFACT_SCAN_V1), no LLM.
    RUNNER_SCAN = "runner_scan", "Test Runner scan"
    # A read-only setup helper's listing (runners without ARTIFACT_SCAN_V1).
    SETUP_AGENT = "setup_agent", "Machine setup agent"


class RequirementKind(models.TextChoices):
    APPLICATION = "application", "Application"
    ARTIFACT = "artifact", "Artifact"
    PATH = "path", "Path"
    CONFIGURATION = "configuration", "Configuration"
    ACCOUNT = "account", "Account"
    SERVICE = "service", "Service"
    OS_SETTING = "os_setting", "OS setting"
    OTHER = "other", "Other"


class RequirementStatus(models.TextChoices):
    UNKNOWN = "unknown", "Unknown"
    PRESENT = "present", "Present"
    MISSING = "missing", "Missing"
    MISMATCH = "mismatch", "Version mismatch"


class RequirementStatusSource(models.TextChoices):
    QUICK_CHECK = "quick_check", "Check machine"
    SETUP_AGENT = "setup_agent", "Machine setup agent"
    PERSON = "person", "Project member"


class RequirementOrigin(models.TextChoices):
    ANALYSIS = "analysis", "Precondition analysis"
    SETUP_AGENT = "setup_agent", "Machine setup agent"
    PERSON = "person", "Project member"


class ArtifactProfile(models.Model):
    """What a project's tests need on the machine and where it comes from.

    Intake is pending while no row exists; a row means intake is done (the
    profile was written, or a person pressed Skip intake).
    """

    project = models.OneToOneField(
        Project, on_delete=models.CASCADE, related_name="artifact_profile"
    )
    base_dir_windows = models.CharField(
        max_length=BASE_DIR_MAX_LENGTH, blank=True, default=""
    )
    base_dir_macos = models.CharField(
        max_length=BASE_DIR_MAX_LENGTH, blank=True, default=""
    )
    base_dir_linux = models.CharField(
        max_length=BASE_DIR_MAX_LENGTH, blank=True, default=""
    )
    # How artifacts are produced and delivered, where credentials are kept
    # (never the values), what not to touch.
    notes = models.TextField(
        max_length=PROFILE_NOTES_MAX_LENGTH, blank=True, default=""
    )
    # 0 until the first write (a row created by Skip intake).
    revision = models.PositiveIntegerField(default=0)
    source = models.CharField(
        max_length=20, choices=ProfileWriteSource.choices, blank=True, default=""
    )
    # The author; for the setup agent, the user whose turn wrote it.
    written_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    written_at = models.DateTimeField(null=True, blank=True)
    change_summary = models.CharField(
        max_length=PROFILE_CHANGE_SUMMARY_MAX_LENGTH, blank=True, default=""
    )
    intake_skipped_at = models.DateTimeField(null=True, blank=True)
    intake_skipped_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Artifacts profile r{self.revision} (project {self.project_id})"


class ArtifactItem(models.Model):
    """One artifact of a profile; the whole set is replaced on each write."""

    profile = models.ForeignKey(
        ArtifactProfile, on_delete=models.CASCADE, related_name="items"
    )
    position = models.PositiveSmallIntegerField()
    # Slug (keys.KEY_RE); requirements and approvals reference it.
    key = models.CharField(max_length=KEY_MAX_LENGTH)
    name = models.CharField(max_length=ITEM_NAME_MAX_LENGTH)
    kind = models.CharField(
        max_length=20, choices=ArtifactKind.choices, default=ArtifactKind.OTHER
    )
    # A glob relative to the base directory, or an absolute path.
    path_pattern = models.CharField(
        max_length=ITEM_PATH_PATTERN_MAX_LENGTH, blank=True, default=""
    )
    # Blank, a version, or LATEST_VERSION.
    expected_version = models.CharField(
        max_length=ITEM_EXPECTED_VERSION_MAX_LENGTH, blank=True, default=""
    )
    # Where it comes from, e.g. "TeamCity › Desktop › Release, artifact app-*.msi".
    source = models.CharField(max_length=ITEM_SOURCE_MAX_LENGTH, blank=True, default="")
    install_hint = models.TextField(
        max_length=INSTALL_HINT_MAX_LENGTH, blank=True, default=""
    )
    # A knowledge base item id, not an FK: a deleted item renders as
    # "KB item #N (deleted)" and never cascades.
    knowledge_item_id = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["profile", "key"], name="artifact_item_unique_key"
            )
        ]

    def __str__(self) -> str:
        return f"{self.key} ({self.kind})"


class ArtifactProfileVersion(models.Model):
    """A replaced profile, kept for restore (the last PROFILE_MAX_VERSIONS)."""

    profile = models.ForeignKey(
        ArtifactProfile, on_delete=models.CASCADE, related_name="versions"
    )
    # The revision this snapshot had while it was current.
    revision = models.PositiveIntegerField()
    # types.ProfileData.to_json() of the whole profile.
    snapshot = models.JSONField(default=dict)
    source = models.CharField(
        max_length=20, choices=ProfileWriteSource.choices, blank=True, default=""
    )
    written_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    written_at = models.DateTimeField(null=True, blank=True)
    change_summary = models.CharField(
        max_length=PROFILE_CHANGE_SUMMARY_MAX_LENGTH, blank=True, default=""
    )
    replaced_at = models.DateTimeField()

    class Meta:
        ordering = ["-revision"]
        constraints = [
            models.UniqueConstraint(
                fields=["profile", "revision"], name="artifact_version_unique"
            )
        ]

    def __str__(self) -> str:
        return f"Artifacts profile revision {self.revision} ({self.profile_id})"


class ArtifactInventory(models.Model):
    """The last listing of one OS's base directory; overwritten on each scan."""

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="artifact_inventories"
    )
    os_name = models.CharField(max_length=10, choices=ArtifactOs.choices)
    base_dir = models.CharField(max_length=BASE_DIR_MAX_LENGTH)
    # The runner's hostname at scan time.
    host = models.CharField(max_length=HOST_MAX_LENGTH, blank=True, default="")
    scanned_at = models.DateTimeField()
    source = models.CharField(
        max_length=20,
        choices=InventorySource.choices,
        default=InventorySource.RUNNER_SCAN,
    )
    scanned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    entry_count = models.PositiveIntegerField(default=0)
    # The runner stopped at the entry, depth or time cap.
    truncated = models.BooleanField(default=False)
    # Number of folders the runner could not read.
    permission_errors = models.PositiveIntegerField(default=0)
    error = models.CharField(
        max_length=INVENTORY_ERROR_MAX_LENGTH, blank=True, default=""
    )
    # [{p, d, s, m}] (types.InventoryEntry): relative path with "/", is a
    # directory, size in bytes, mtime as epoch seconds. At most
    # ARTIFACT_SCAN_MAX_ENTRIES; defer it on display loads.
    entries = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "os_name"], name="artifact_inventory_one_per_os"
            )
        ]

    def __str__(self) -> str:
        return f"Inventory {self.os_name} (project {self.project_id})"


class Stage0Requirement(models.Model):
    """One thing a case's preconditions assume is on the machine.

    The status is trusted until changed (plan D5): it stays until a check, the
    agent or a person records otherwise; a different ``checked_host`` than the
    connected runner's reads as unknown.
    """

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="stage0_requirements"
    )
    # Slug (keys.KEY_RE), unique per project.
    key = models.CharField(max_length=KEY_MAX_LENGTH)
    title = models.CharField(max_length=REQUIREMENT_TITLE_MAX_LENGTH)
    kind = models.CharField(
        max_length=20, choices=RequirementKind.choices, default=RequirementKind.OTHER
    )
    # An app name, a path or an artifact key; never reaches a shell.
    check_target = models.CharField(
        max_length=REQUIREMENT_CHECK_TARGET_MAX_LENGTH, blank=True, default=""
    )
    install_hint = models.TextField(
        max_length=INSTALL_HINT_MAX_LENGTH, blank=True, default=""
    )
    # Keys of requirements that cannot be in place together with this one.
    conflicts_with = models.JSONField(default=list, blank=True)
    ignored = models.BooleanField(default=False)
    status = models.CharField(
        max_length=10,
        choices=RequirementStatus.choices,
        default=RequirementStatus.UNKNOWN,
    )
    status_detail = models.CharField(
        max_length=REQUIREMENT_STATUS_DETAIL_MAX_LENGTH, blank=True, default=""
    )
    status_source = models.CharField(
        max_length=20, choices=RequirementStatusSource.choices, blank=True, default=""
    )
    checked_at = models.DateTimeField(null=True, blank=True)
    # The runner's hostname when the status was recorded.
    checked_host = models.CharField(max_length=HOST_MAX_LENGTH, blank=True, default="")
    # The person who recorded it, or whose turn or check did.
    checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    origin = models.CharField(
        max_length=20,
        choices=RequirementOrigin.choices,
        default=RequirementOrigin.ANALYSIS,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "key"], name="stage0_requirement_unique_key"
            )
        ]

    def __str__(self) -> str:
        return f"{self.key} ({self.status})"


class PreconditionAnalysis(models.Model):
    """The S0 extraction of one distinct precondition text of a project.

    Keyed by ``TestCase.preconditions_md5`` (MD5 of the trimmed text), so
    editing a case's preconditions makes it unanalysed again.
    """

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="precondition_analyses"
    )
    preconditions_md5 = models.CharField(max_length=32)
    analysed_at = models.DateTimeField()
    requirements = models.ManyToManyField(
        Stage0Requirement, blank=True, related_name="analyses"
    )
    # What the analysis could not turn into a requirement.
    unclear = models.CharField(
        max_length=ANALYSIS_UNCLEAR_MAX_LENGTH, blank=True, default=""
    )
    # The text assumes nothing on the machine (or is empty).
    none_needed = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "preconditions_md5"],
                name="precondition_analysis_unique_md5",
            )
        ]

    def __str__(self) -> str:
        return f"Analysis {self.preconditions_md5} (project {self.project_id})"
