"""Versioned writes of a project's application context notes.

Every writer of ``Project.project_prompt`` (the context form, the wizard, AI
refine, the machine setup agent, restore) goes through
``write_application_context``, so each replaced value lands in the history.

Imports only models and exceptions, so ``projects.services`` and
``machine_setup`` can both use it without an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from accounts.models import CustomUser
from projects.exceptions import (
    ApplicationContextConflictError,
    ApplicationContextTooLongError,
    ApplicationContextVersionNotFoundError,
)
from projects.models import (
    PROJECT_PROMPT_MAX_LENGTH,
    ApplicationContextVersion,
    ContextWriteSource,
    Project,
)

CONTEXT_HISTORY_LIMIT = 20


@dataclass(frozen=True)
class ApplicationContextSnapshot:
    text: str
    revision: int
    source: str
    max_length: int


@dataclass(frozen=True)
class ContextProvenance:
    """Who wrote the current notes, for the provenance line on the pages."""

    revision: int
    source: str
    source_label: str
    author_name: str
    written_at: datetime | None
    change_summary: str


def get_context_provenance(project: Project) -> ContextProvenance | None:
    """``None`` when the notes were never written through a versioned write.

    One query for the author's name, only when the notes have an author.
    """
    if not project.project_prompt_source:
        return None
    author_name = ""
    if project.project_prompt_written_by_id is not None:
        author = (
            CustomUser.objects.filter(pk=project.project_prompt_written_by_id)
            .only("first_name", "last_name", "email")
            .first()
        )
        if author is not None:
            author_name = author.get_full_name() or author.email
    return ContextProvenance(
        revision=project.project_prompt_revision,
        source=project.project_prompt_source,
        source_label=ContextWriteSource(project.project_prompt_source).label,
        author_name=author_name,
        written_at=project.project_prompt_written_at,
        change_summary=project.project_prompt_change_summary,
    )


def get_application_context(project_id: int) -> ApplicationContextSnapshot:
    text, revision, source = (
        Project.objects.filter(pk=project_id)
        .values_list(
            "project_prompt", "project_prompt_revision", "project_prompt_source"
        )
        .get()
    )
    return ApplicationContextSnapshot(
        text=text,
        revision=revision,
        source=source,
        max_length=PROJECT_PROMPT_MAX_LENGTH,
    )


def write_application_context(
    *,
    project_id: int,
    text: str,
    source: ContextWriteSource,
    author: CustomUser | None,
    change_summary: str = "",
    expected_revision: int | None = None,
) -> int:
    """Replace the notes, keeping the old value in the history; returns the revision.

    ``expected_revision`` makes the write conditional (the setup agent): it
    raises ``ApplicationContextConflictError`` if the notes moved since then.
    Writing the current text again is a no-op.
    """
    if len(text) > PROJECT_PROMPT_MAX_LENGTH:
        raise ApplicationContextTooLongError(len(text), PROJECT_PROMPT_MAX_LENGTH)
    with transaction.atomic():
        current = (
            Project.objects.select_for_update()
            .only(
                "project_prompt",
                "project_prompt_revision",
                "project_prompt_source",
                "project_prompt_written_by",
                "project_prompt_written_at",
                "project_prompt_change_summary",
            )
            .get(pk=project_id)
        )
        if (
            expected_revision is not None
            and expected_revision != current.project_prompt_revision
        ):
            raise ApplicationContextConflictError(
                expected_revision, current.project_prompt_revision
            )
        if text == current.project_prompt:
            return current.project_prompt_revision

        now = timezone.now()
        ApplicationContextVersion.objects.create(
            project_id=project_id,
            revision=current.project_prompt_revision,
            text=current.project_prompt,
            written_by_source=current.project_prompt_source,
            written_by_id=current.project_prompt_written_by_id,
            written_at=current.project_prompt_written_at,
            change_summary=current.project_prompt_change_summary,
            replaced_at=now,
        )
        Project.objects.filter(pk=project_id).update(
            project_prompt=text,
            project_prompt_revision=F("project_prompt_revision") + 1,
            project_prompt_source=source,
            project_prompt_written_by=author,
            project_prompt_written_at=now,
            project_prompt_change_summary=change_summary[:500],
            updated_at=now,
        )
        _prune_history(project_id)
        return current.project_prompt_revision + 1


def list_context_versions(project: Project) -> list[ApplicationContextVersion]:
    return list(
        ApplicationContextVersion.objects.filter(project=project)
        .select_related("written_by")
        .order_by("-revision")[:CONTEXT_HISTORY_LIMIT]
    )


def restore_context_version(
    *, project: Project, version_id: int, user: CustomUser
) -> int:
    """Write an earlier version back as a new revision (so a restore is undoable)."""
    text = (
        ApplicationContextVersion.objects.filter(pk=version_id, project=project)
        .values_list("text", flat=True)
        .first()
    )
    if text is None:
        raise ApplicationContextVersionNotFoundError()
    return write_application_context(
        project_id=project.pk,
        text=text,
        source=ContextWriteSource.RESTORE,
        author=user,
    )


def _prune_history(project_id: int) -> None:
    keep = (
        ApplicationContextVersion.objects.filter(project_id=project_id)
        .order_by("-revision")
        .values_list("pk", flat=True)[:CONTEXT_HISTORY_LIMIT]
    )
    ApplicationContextVersion.objects.filter(project_id=project_id).exclude(
        pk__in=list(keep)
    ).delete()
