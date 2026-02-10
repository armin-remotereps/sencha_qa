from __future__ import annotations

from dataclasses import asdict

from django.core.paginator import Page, Paginator
from django.db import transaction
from django.db.models import QuerySet

from accounts.models import CustomUser
from projects.models import Project, Tag, TestCase, TestCaseData


@transaction.atomic
def create_project(*, user: CustomUser, name: str, tag_names: list[str]) -> Project:
    project = Project.objects.create(name=name)
    _sync_tags(project, tag_names)
    project.members.add(user)
    return project


@transaction.atomic
def update_project(*, project: Project, name: str, tag_names: list[str]) -> Project:
    project.name = name
    project.save()
    _sync_tags(project, tag_names)
    return project


def archive_project(project: Project) -> None:
    project.archived = True
    project.save()


def unarchive_project(project: Project) -> None:
    project.archived = False
    project.save()


def get_project_for_user(project_id: int, user: CustomUser) -> Project | None:
    try:
        return Project.objects.filter(id=project_id, archived=False, members=user).get()
    except Project.DoesNotExist:
        return None


def get_project_by_id(project_id: int, user: CustomUser) -> Project | None:
    try:
        return Project.objects.filter(id=project_id, members=user).get()
    except Project.DoesNotExist:
        return None


def list_projects_for_user(
    *,
    user: CustomUser,
    search: str | None,
    tag_filter: str | None,
    page: int,
    per_page: int,
) -> Page[Project]:
    qs: QuerySet[Project] = Project.objects.filter(
        members=user, archived=False
    ).order_by("-created_at")

    if search:
        qs = qs.filter(name__icontains=search)

    if tag_filter:
        qs = qs.filter(tags__name=tag_filter)

    paginator: Paginator[Project] = Paginator(qs, per_page)
    return paginator.get_page(page)


def get_all_tags_for_user(user: CustomUser) -> QuerySet[Tag]:
    return (
        Tag.objects.filter(projects__members=user, projects__archived=False)
        .distinct()
        .order_by("name")
    )


def _normalize_tag_names(tag_names: list[str]) -> list[str]:
    return list({name.strip().lower() for name in tag_names if name.strip()})


def _get_or_create_tags(names: list[str]) -> list[Tag]:
    tags: list[Tag] = []
    for name in names:
        tag, _ = Tag.objects.get_or_create(name=name)
        tags.append(tag)
    return tags


def _sync_tags(project: Project, tag_names: list[str]) -> None:
    normalized = _normalize_tag_names(tag_names)
    tags = _get_or_create_tags(normalized)
    project.tags.set(tags)


# ============================================================================
# TEST CASE SERVICES
# ============================================================================


def create_test_case(*, project: Project, data: TestCaseData) -> TestCase:
    return TestCase.objects.create(project=project, **asdict(data))


def update_test_case(*, test_case: TestCase, data: TestCaseData) -> TestCase:
    for field, value in asdict(data).items():
        setattr(test_case, field, value)
    test_case.save()
    return test_case


def delete_test_case(test_case: TestCase) -> None:
    test_case.delete()


def get_test_case_for_project(test_case_id: int, project: Project) -> TestCase | None:
    try:
        return TestCase.objects.filter(id=test_case_id, project=project).get()
    except TestCase.DoesNotExist:
        return None


def list_test_cases_for_project(
    *,
    project: Project,
    search: str | None,
    page: int,
    per_page: int,
) -> Page[TestCase]:
    qs: QuerySet[TestCase] = TestCase.objects.filter(project=project).order_by(
        "-created_at"
    )

    if search:
        qs = qs.filter(title__icontains=search)

    paginator: Paginator[TestCase] = Paginator(qs, per_page)
    return paginator.get_page(page)
