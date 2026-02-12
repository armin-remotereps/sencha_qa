import base64
from unittest.mock import MagicMock, patch

from django.contrib.admin.sites import AdminSite
from django.db import IntegrityError
from django.test import RequestFactory, TestCase
from django.urls import reverse

from accounts.models import CustomUser
from projects.admin import (
    ProjectAdmin,
    TagAdmin,
    TestCaseAdmin,
    TestRunAdmin,
    TestRunScreenshotAdmin,
    TestRunTestCaseAdmin,
)
from projects.forms import ProjectForm, TestCaseForm
from projects.models import (
    Project,
    Tag,
)
from projects.models import TestCase as TestCaseModel
from projects.models import (
    TestCaseData,
    TestCasePriority,
    TestCaseType,
    TestCaseUpload,
    TestRun,
    TestRunScreenshot,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
    UploadStatus,
)
from projects.services import (
    _build_log_callback,
    _build_screenshot_callback,
    _build_task_description,
    _extract_agent_summary,
    _fetch_pivot,
    _finalize_pivot,
    _mark_pivot_failed,
    _mark_pivot_in_progress,
    _update_test_run_status_if_needed,
    archive_project,
    create_project,
    create_test_case,
    delete_test_case,
    execute_test_run_test_case,
    get_all_tags_for_user,
    get_project_by_id,
    get_project_for_user,
    get_test_case_for_project,
    list_projects_for_user,
    list_test_cases_for_project,
    unarchive_project,
    update_project,
    update_test_case,
)

# ============================================================================
# MODEL TESTS
# ============================================================================


class TagModelTests(TestCase):
    """Tests for Tag model basic functionality."""

    def test_should_create_tag_with_name(self) -> None:
        tag = Tag.objects.create(name="backend")
        self.assertEqual(tag.name, "backend")
        self.assertIsNotNone(tag.id)

    def test_should_return_name_in_str_representation(self) -> None:
        tag = Tag.objects.create(name="frontend")
        self.assertEqual(str(tag), "frontend")

    def test_should_enforce_unique_name_constraint(self) -> None:
        Tag.objects.create(name="api")
        with self.assertRaises(IntegrityError):
            Tag.objects.create(name="api")


class ProjectModelTests(TestCase):
    """Tests for Project model basic functionality."""

    def test_should_create_project_with_required_fields(self) -> None:
        project = Project.objects.create(name="Test Project")
        self.assertEqual(project.name, "Test Project")
        self.assertIsNotNone(project.id)

    def test_should_default_archived_to_false(self) -> None:
        project = Project.objects.create(name="Active Project")
        self.assertFalse(project.archived)

    def test_should_set_created_and_updated_timestamps(self) -> None:
        project = Project.objects.create(name="Timestamped Project")
        self.assertIsNotNone(project.created_at)
        self.assertIsNotNone(project.updated_at)

    def test_should_return_name_in_str_representation(self) -> None:
        project = Project.objects.create(name="My Project")
        self.assertEqual(str(project), "My Project")

    def test_should_support_many_to_many_tags(self) -> None:
        project = Project.objects.create(name="Tagged Project")
        tag1 = Tag.objects.create(name="python")
        tag2 = Tag.objects.create(name="django")
        project.tags.add(tag1, tag2)
        self.assertEqual(project.tags.count(), 2)
        self.assertIn(tag1, project.tags.all())
        self.assertIn(tag2, project.tags.all())

    def test_should_support_many_to_many_members(self) -> None:
        project = Project.objects.create(name="Team Project")
        user1 = CustomUser.objects.create_user(
            email="user1@example.com",
            password="pass123",
        )
        user2 = CustomUser.objects.create_user(
            email="user2@example.com",
            password="pass123",
        )
        project.members.add(user1, user2)
        self.assertEqual(project.members.count(), 2)
        self.assertIn(user1, project.members.all())
        self.assertIn(user2, project.members.all())


# ============================================================================
# SERVICE LAYER TESTS - Core business logic
# ============================================================================


class CreateProjectServiceTests(TestCase):
    """Tests for create_project service function."""

    def setUp(self) -> None:
        self.user = CustomUser.objects.create_user(
            email="creator@example.com",
            password="pass123",
            first_name="John",
            last_name="Doe",
        )

    def test_should_create_project_and_add_creator_as_member(self) -> None:
        project = create_project(
            user=self.user,
            name="New Project",
            tag_names=["backend", "api"],
        )
        self.assertEqual(project.name, "New Project")
        self.assertIn(self.user, project.members.all())

    def test_should_create_new_tags_when_not_exist(self) -> None:
        project = create_project(
            user=self.user,
            name="Tagged Project",
            tag_names=["newTag1", "newTag2"],
        )
        self.assertEqual(Tag.objects.filter(name="newtag1").count(), 1)
        self.assertEqual(Tag.objects.filter(name="newtag2").count(), 1)
        self.assertEqual(project.tags.count(), 2)

    def test_should_reuse_existing_tags_by_normalized_name(self) -> None:
        Tag.objects.create(name="existing")
        initial_tag_count = Tag.objects.count()

        project = create_project(
            user=self.user,
            name="Reusing Tags",
            tag_names=["Existing", "EXISTING", "  existing  "],
        )

        # Should only have 1 tag, not 4 (1 initial + 1 added, not 3)
        self.assertEqual(Tag.objects.count(), initial_tag_count)
        self.assertEqual(project.tags.count(), 1)
        self.assertEqual(project.tags.first().name, "existing")  # type: ignore[union-attr]

    def test_should_handle_empty_tags_list(self) -> None:
        project = create_project(
            user=self.user,
            name="No Tags Project",
            tag_names=[],
        )
        self.assertEqual(project.tags.count(), 0)

    def test_should_normalize_tag_names_before_lookup(self) -> None:
        project = create_project(
            user=self.user,
            name="Normalized Tags",
            tag_names=["  Python  ", "DJANGO", "Rest-API"],
        )
        tag_names = [tag.name for tag in project.tags.all()]
        self.assertIn("python", tag_names)
        self.assertIn("django", tag_names)
        self.assertIn("rest-api", tag_names)


class UpdateProjectServiceTests(TestCase):
    """Tests for update_project service function."""

    def setUp(self) -> None:
        self.user = CustomUser.objects.create_user(
            email="owner@example.com",
            password="pass123",
        )
        self.project = create_project(
            user=self.user,
            name="Original Name",
            tag_names=["oldtag1", "oldtag2"],
        )

    def test_should_update_project_name(self) -> None:
        updated = update_project(
            project=self.project,
            name="Updated Name",
            tag_names=["oldtag1", "oldtag2"],
        )
        self.assertEqual(updated.name, "Updated Name")

    def test_should_sync_tags_on_update(self) -> None:
        updated = update_project(
            project=self.project,
            name="Same Name",
            tag_names=["newtag1", "newtag2"],
        )
        tag_names = [tag.name for tag in updated.tags.all()]
        self.assertIn("newtag1", tag_names)
        self.assertIn("newtag2", tag_names)
        self.assertEqual(updated.tags.count(), 2)

    def test_should_remove_old_tags_when_updating(self) -> None:
        update_project(
            project=self.project,
            name="Same Name",
            tag_names=["totallynew"],
        )
        self.project.refresh_from_db()
        tag_names = [tag.name for tag in self.project.tags.all()]
        self.assertNotIn("oldtag1", tag_names)
        self.assertNotIn("oldtag2", tag_names)

    def test_should_create_new_tags_during_update(self) -> None:
        initial_tag_count = Tag.objects.count()
        update_project(
            project=self.project,
            name="Same Name",
            tag_names=["brandnewtag"],
        )
        self.assertGreater(Tag.objects.count(), initial_tag_count)
        self.assertTrue(Tag.objects.filter(name="brandnewtag").exists())


class ArchiveProjectServiceTests(TestCase):
    """Tests for archive_project service function."""

    def setUp(self) -> None:
        self.user = CustomUser.objects.create_user(
            email="archiver@example.com",
            password="pass123",
        )
        self.project = create_project(
            user=self.user,
            name="To Be Archived",
            tag_names=[],
        )

    def test_should_set_archived_to_true(self) -> None:
        archive_project(self.project)
        self.project.refresh_from_db()
        self.assertTrue(self.project.archived)

    def test_should_not_delete_project_from_database(self) -> None:
        project_id = self.project.id
        archive_project(self.project)
        self.assertTrue(Project.objects.filter(id=project_id).exists())


class UnarchiveProjectServiceTests(TestCase):
    """Tests for unarchive_project service function."""

    def setUp(self) -> None:
        self.user = CustomUser.objects.create_user(
            email="unarchiver@example.com",
            password="pass123",
        )
        self.project = create_project(
            user=self.user,
            name="Archived Project",
            tag_names=[],
        )
        archive_project(self.project)

    def test_should_set_archived_to_false(self) -> None:
        unarchive_project(self.project)
        self.project.refresh_from_db()
        self.assertFalse(self.project.archived)


class GetProjectForUserServiceTests(TestCase):
    """Tests for get_project_for_user service function."""

    def setUp(self) -> None:
        self.owner = CustomUser.objects.create_user(
            email="owner@example.com",
            password="pass123",
        )
        self.non_member = CustomUser.objects.create_user(
            email="nonmember@example.com",
            password="pass123",
        )
        self.project = create_project(
            user=self.owner,
            name="Access Test Project",
            tag_names=[],
        )

    def test_should_return_project_when_user_is_member(self) -> None:
        result = get_project_for_user(self.project.id, self.owner)
        self.assertIsNotNone(result)
        self.assertEqual(result.id, self.project.id)  # type: ignore[union-attr]

    def test_should_return_none_when_user_not_member(self) -> None:
        result = get_project_for_user(self.project.id, self.non_member)
        self.assertIsNone(result)

    def test_should_return_none_when_project_archived(self) -> None:
        archive_project(self.project)
        result = get_project_for_user(self.project.id, self.owner)
        self.assertIsNone(result)

    def test_should_return_none_when_project_does_not_exist(self) -> None:
        result = get_project_for_user(99999, self.owner)
        self.assertIsNone(result)


class GetProjectByIdServiceTests(TestCase):
    """Tests for get_project_by_id service function (admin-level access)."""

    def setUp(self) -> None:
        self.owner = CustomUser.objects.create_user(
            email="owner@example.com",
            password="pass123",
        )
        self.non_member = CustomUser.objects.create_user(
            email="nonmember@example.com",
            password="pass123",
        )
        self.project = create_project(
            user=self.owner,
            name="Admin Access Project",
            tag_names=[],
        )

    def test_should_return_project_when_user_is_member_even_if_archived(self) -> None:
        archive_project(self.project)
        result = get_project_by_id(self.project.id, self.owner)
        self.assertIsNotNone(result)
        self.assertEqual(result.id, self.project.id)  # type: ignore[union-attr]

    def test_should_return_none_when_user_not_member(self) -> None:
        result = get_project_by_id(self.project.id, self.non_member)
        self.assertIsNone(result)


class ListProjectsForUserServiceTests(TestCase):
    """Tests for list_projects_for_user service function."""

    def setUp(self) -> None:
        self.user = CustomUser.objects.create_user(
            email="lister@example.com",
            password="pass123",
        )
        self.other_user = CustomUser.objects.create_user(
            email="other@example.com",
            password="pass123",
        )
        # User's projects
        self.project1 = create_project(
            user=self.user,
            name="Alpha Project",
            tag_names=["python", "backend"],
        )
        self.project2 = create_project(
            user=self.user,
            name="Beta Project",
            tag_names=["javascript", "frontend"],
        )
        # Other user's project
        self.other_project = create_project(
            user=self.other_user,
            name="Other Project",
            tag_names=[],
        )
        # Archived project
        self.archived_project = create_project(
            user=self.user,
            name="Archived Project",
            tag_names=["old"],
        )
        archive_project(self.archived_project)

    def test_should_return_only_non_archived_projects_for_member(self) -> None:
        result = list_projects_for_user(
            user=self.user,
            search=None,
            tag_filter=None,
            page=1,
            per_page=10,
        )
        project_ids = [p.id for p in result]
        self.assertIn(self.project1.id, project_ids)
        self.assertIn(self.project2.id, project_ids)
        self.assertNotIn(self.archived_project.id, project_ids)
        self.assertEqual(len(result), 2)

    def test_should_exclude_projects_where_user_not_member(self) -> None:
        result = list_projects_for_user(
            user=self.user,
            search=None,
            tag_filter=None,
            page=1,
            per_page=10,
        )
        project_ids = [p.id for p in result]
        self.assertNotIn(self.other_project.id, project_ids)

    def test_should_filter_by_name_case_insensitive(self) -> None:
        result = list_projects_for_user(
            user=self.user,
            search="alpha",
            tag_filter=None,
            page=1,
            per_page=10,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "Alpha Project")

    def test_should_filter_by_tag_name(self) -> None:
        result = list_projects_for_user(
            user=self.user,
            search=None,
            tag_filter="python",
            page=1,
            per_page=10,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "Alpha Project")

    def test_should_return_empty_when_no_projects(self) -> None:
        new_user = CustomUser.objects.create_user(
            email="newbie@example.com",
            password="pass123",
        )
        result = list_projects_for_user(
            user=new_user,
            search=None,
            tag_filter=None,
            page=1,
            per_page=10,
        )
        self.assertEqual(len(result), 0)

    def test_should_support_pagination(self) -> None:
        # Create more projects
        for i in range(5):
            create_project(
                user=self.user,
                name=f"Project {i}",
                tag_names=[],
            )

        page1 = list_projects_for_user(
            user=self.user,
            search=None,
            tag_filter=None,
            page=1,
            per_page=3,
        )
        self.assertEqual(len(page1), 3)

        page2 = list_projects_for_user(
            user=self.user,
            search=None,
            tag_filter=None,
            page=2,
            per_page=3,
        )
        self.assertEqual(len(page2), 3)

        # Ensure different results
        page1_ids = [p.id for p in page1]
        page2_ids = [p.id for p in page2]
        self.assertNotEqual(page1_ids, page2_ids)


class GetAllTagsForUserServiceTests(TestCase):
    """Tests for get_all_tags_for_user service function."""

    def setUp(self) -> None:
        self.user = CustomUser.objects.create_user(
            email="taglister@example.com",
            password="pass123",
        )
        self.other_user = CustomUser.objects.create_user(
            email="othertagger@example.com",
            password="pass123",
        )

    def test_should_return_unique_tags_from_user_projects(self) -> None:
        create_project(
            user=self.user,
            name="Project 1",
            tag_names=["python", "django"],
        )
        create_project(
            user=self.user,
            name="Project 2",
            tag_names=["python", "celery"],
        )
        tags = get_all_tags_for_user(self.user)
        tag_names = [tag.name for tag in tags]
        self.assertIn("python", tag_names)
        self.assertIn("django", tag_names)
        self.assertIn("celery", tag_names)
        # Should have exactly 3 unique tags
        self.assertEqual(len(tags), 3)

    def test_should_exclude_tags_from_archived_projects(self) -> None:
        create_project(
            user=self.user,
            name="Active Project",
            tag_names=["active"],
        )
        archived = create_project(
            user=self.user,
            name="Archived Project",
            tag_names=["archived"],
        )
        archive_project(archived)

        tags = get_all_tags_for_user(self.user)
        tag_names = [tag.name for tag in tags]
        self.assertIn("active", tag_names)
        self.assertNotIn("archived", tag_names)

    def test_should_return_empty_when_user_has_no_projects(self) -> None:
        tags = get_all_tags_for_user(self.user)
        self.assertEqual(len(tags), 0)


# ============================================================================
# FORM TESTS
# ============================================================================


class ProjectFormTests(TestCase):
    """Tests for ProjectForm validation and cleaning."""

    def test_should_accept_valid_name_and_tags(self) -> None:
        form = ProjectForm(
            data={
                "name": "Valid Project",
                "tags": "python, django, celery",
            }
        )
        self.assertTrue(form.is_valid())

    def test_should_require_name_field(self) -> None:
        form = ProjectForm(
            data={
                "tags": "python",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("name", form.errors)

    def test_should_parse_comma_separated_tags(self) -> None:
        form = ProjectForm(
            data={
                "name": "Tagged Project",
                "tags": "tag1, tag2, tag3",
            }
        )
        self.assertTrue(form.is_valid())
        cleaned_tags = form.cleaned_data["tags"]
        self.assertEqual(len(cleaned_tags), 3)
        self.assertIn("tag1", cleaned_tags)
        self.assertIn("tag2", cleaned_tags)
        self.assertIn("tag3", cleaned_tags)

    def test_should_strip_whitespace_from_tags(self) -> None:
        form = ProjectForm(
            data={
                "name": "Stripped Project",
                "tags": "  Python  , DJANGO , Rest-API ",
            }
        )
        self.assertTrue(form.is_valid())
        cleaned_tags = form.cleaned_data["tags"]
        self.assertIn("Python", cleaned_tags)
        self.assertIn("DJANGO", cleaned_tags)
        self.assertIn("Rest-API", cleaned_tags)

    def test_should_handle_empty_tags_field(self) -> None:
        form = ProjectForm(
            data={
                "name": "No Tags Project",
                "tags": "",
            }
        )
        self.assertTrue(form.is_valid())
        cleaned_tags = form.cleaned_data["tags"]
        self.assertEqual(len(cleaned_tags), 0)

    def test_should_handle_tags_with_extra_commas_and_spaces(self) -> None:
        form = ProjectForm(
            data={
                "name": "Messy Tags Project",
                "tags": "  , tag1 ,, tag2  , , tag3,  ",
            }
        )
        self.assertTrue(form.is_valid())
        cleaned_tags = form.cleaned_data["tags"]
        # Should have exactly 3 tags, ignoring empty strings
        self.assertEqual(len(cleaned_tags), 3)
        self.assertIn("tag1", cleaned_tags)
        self.assertIn("tag2", cleaned_tags)
        self.assertIn("tag3", cleaned_tags)


# ============================================================================
# VIEW TESTS
# ============================================================================


class ProjectListViewTests(TestCase):
    """Tests for project list view."""

    def setUp(self) -> None:
        self.user = CustomUser.objects.create_user(
            email="viewer@example.com",
            password="pass123",
        )
        self.other_user = CustomUser.objects.create_user(
            email="other@example.com",
            password="pass123",
        )
        self.project = create_project(
            user=self.user,
            name="My Project",
            tag_names=["python"],
        )
        self.other_project = create_project(
            user=self.other_user,
            name="Other Project",
            tag_names=[],
        )
        self.archived_project = create_project(
            user=self.user,
            name="Archived Project",
            tag_names=[],
        )
        archive_project(self.archived_project)

    def test_should_redirect_when_unauthenticated(self) -> None:
        response = self.client.get(reverse("projects:list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)  # type: ignore[attr-defined]

    def test_should_return_200_with_correct_template_when_authenticated(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get(reverse("projects:list"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "projects/list.html")

    def test_should_show_only_user_non_archived_projects(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get(reverse("projects:list"))
        projects = response.context["projects"]
        project_ids = [p.id for p in projects]
        self.assertIn(self.project.id, project_ids)
        self.assertNotIn(self.archived_project.id, project_ids)

    def test_should_not_show_other_users_projects(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get(reverse("projects:list"))
        projects = response.context["projects"]
        project_ids = [p.id for p in projects]
        self.assertNotIn(self.other_project.id, project_ids)

    def test_should_not_show_archived_projects(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get(reverse("projects:list"))
        projects = response.context["projects"]
        project_ids = [p.id for p in projects]
        self.assertNotIn(self.archived_project.id, project_ids)


class ProjectCreateViewTests(TestCase):
    """Tests for project creation view."""

    def setUp(self) -> None:
        self.user = CustomUser.objects.create_user(
            email="creator@example.com",
            password="pass123",
        )

    def test_should_redirect_when_unauthenticated(self) -> None:
        response = self.client.post(
            reverse("projects:create"),
            {"name": "New Project", "tags": "python"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)  # type: ignore[attr-defined]

    def test_should_return_405_for_get_request(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get(reverse("projects:create"))
        self.assertEqual(response.status_code, 405)

    def test_should_create_project_and_redirect_on_post(self) -> None:
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("projects:create"),
            {"name": "New Project", "tags": "python, django"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse("projects:list"))
        self.assertTrue(Project.objects.filter(name="New Project").exists())

    def test_should_add_current_user_as_member(self) -> None:
        self.client.force_login(self.user)
        self.client.post(
            reverse("projects:create"),
            {"name": "User Project", "tags": ""},
        )
        project = Project.objects.get(name="User Project")
        self.assertIn(self.user, project.members.all())


class ProjectEditViewTests(TestCase):
    """Tests for project edit view."""

    def setUp(self) -> None:
        self.user = CustomUser.objects.create_user(
            email="editor@example.com",
            password="pass123",
        )
        self.non_member = CustomUser.objects.create_user(
            email="nonmember@example.com",
            password="pass123",
        )
        self.project = create_project(
            user=self.user,
            name="Original Name",
            tag_names=["python"],
        )

    def test_should_redirect_when_unauthenticated(self) -> None:
        response = self.client.post(
            reverse("projects:edit", args=[self.project.id]),
            {"name": "Updated Name", "tags": "python"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)  # type: ignore[attr-defined]

    def test_should_return_405_for_get_request(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get(reverse("projects:edit", args=[self.project.id]))
        self.assertEqual(response.status_code, 405)

    def test_should_update_project_and_redirect_on_post(self) -> None:
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("projects:edit", args=[self.project.id]),
            {"name": "Updated Name", "tags": "django"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse("projects:list"))
        self.project.refresh_from_db()
        self.assertEqual(self.project.name, "Updated Name")

    def test_should_return_404_when_user_not_member(self) -> None:
        self.client.force_login(self.non_member)
        response = self.client.post(
            reverse("projects:edit", args=[self.project.id]),
            {"name": "Hacked Name", "tags": ""},
        )
        self.assertEqual(response.status_code, 404)

    def test_should_return_404_when_project_does_not_exist(self) -> None:
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("projects:edit", args=[99999]),
            {"name": "Name", "tags": ""},
        )
        self.assertEqual(response.status_code, 404)


class ProjectArchiveViewTests(TestCase):
    """Tests for project archive view."""

    def setUp(self) -> None:
        self.user = CustomUser.objects.create_user(
            email="archiver@example.com",
            password="pass123",
        )
        self.non_member = CustomUser.objects.create_user(
            email="nonmember@example.com",
            password="pass123",
        )
        self.project = create_project(
            user=self.user,
            name="To Archive",
            tag_names=[],
        )

    def test_should_redirect_when_unauthenticated(self) -> None:
        response = self.client.post(reverse("projects:archive", args=[self.project.id]))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)  # type: ignore[attr-defined]

    def test_should_return_405_for_get_request(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get(reverse("projects:archive", args=[self.project.id]))
        self.assertEqual(response.status_code, 405)

    def test_should_archive_project_and_redirect_on_post(self) -> None:
        self.client.force_login(self.user)
        response = self.client.post(reverse("projects:archive", args=[self.project.id]))
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse("projects:list"))
        self.project.refresh_from_db()
        self.assertTrue(self.project.archived)

    def test_should_return_404_when_user_not_member(self) -> None:
        self.client.force_login(self.non_member)
        response = self.client.post(reverse("projects:archive", args=[self.project.id]))
        self.assertEqual(response.status_code, 404)


# ============================================================================
# ADMIN TESTS
# ============================================================================


class AdminRegistrationTests(TestCase):
    """Tests for admin registration of models."""

    def test_should_register_tag_in_admin(self) -> None:
        site = AdminSite()
        admin = TagAdmin(Tag, site)
        self.assertIsNotNone(admin)

    def test_should_register_project_in_admin(self) -> None:
        site = AdminSite()
        admin = ProjectAdmin(Project, site)
        self.assertIsNotNone(admin)


class ProjectAdminActionTests(TestCase):
    """Tests for ProjectAdmin custom actions."""

    def setUp(self) -> None:
        self.site = AdminSite()
        self.admin = ProjectAdmin(Project, self.site)
        self.factory = RequestFactory()
        self.user = CustomUser.objects.create_user(
            email="admin@example.com",
            password="pass123",
        )
        self.project1 = create_project(
            user=self.user,
            name="Project 1",
            tag_names=[],
        )
        self.project2 = create_project(
            user=self.user,
            name="Project 2",
            tag_names=[],
        )

    def test_should_have_archive_projects_action(self) -> None:
        request = self.factory.get("/admin/")
        request.user = self.user
        actions = self.admin.get_actions(request)
        self.assertIn("archive_projects", actions)

    def test_should_have_unarchive_projects_action(self) -> None:
        request = self.factory.get("/admin/")
        request.user = self.user
        actions = self.admin.get_actions(request)
        self.assertIn("unarchive_projects", actions)

    def test_should_archive_selected_projects(self) -> None:
        queryset = Project.objects.filter(id__in=[self.project1.id, self.project2.id])
        self.admin.archive_projects(request=None, queryset=queryset)

        self.project1.refresh_from_db()
        self.project2.refresh_from_db()
        self.assertTrue(self.project1.archived)
        self.assertTrue(self.project2.archived)

    def test_should_unarchive_selected_projects(self) -> None:
        archive_project(self.project1)
        archive_project(self.project2)

        queryset = Project.objects.filter(id__in=[self.project1.id, self.project2.id])
        self.admin.unarchive_projects(request=None, queryset=queryset)

        self.project1.refresh_from_db()
        self.project2.refresh_from_db()
        self.assertFalse(self.project1.archived)
        self.assertFalse(self.project2.archived)


# ============================================================================
# TEST CASE MODEL TESTS
# ============================================================================


class TestCaseModelTests(TestCase):
    """Tests for TestCase model basic functionality."""

    def setUp(self) -> None:
        self.user = CustomUser.objects.create_user(
            email="testuser@example.com",
            password="testpass123",
        )
        self.project = create_project(user=self.user, name="Test Project", tag_names=[])

    def test_should_create_test_case_with_all_fields(self) -> None:
        """Verify test case can be created with all fields populated."""
        test_case = TestCaseModel.objects.create(
            project=self.project,
            testrail_id="C12345",
            title="Login with valid credentials",
            template="Test Case Template",
            type=TestCaseType.FUNCTIONAL,
            priority=TestCasePriority.MUST_TEST_HIGH,
            estimate="30m",
            references="JIRA-123",
            preconditions="User is on login page",
            steps="1. Enter email\n2. Enter password\n3. Click login",
            expected="User is logged in successfully",
        )
        self.assertEqual(test_case.project, self.project)
        self.assertEqual(test_case.testrail_id, "C12345")
        self.assertEqual(test_case.title, "Login with valid credentials")
        self.assertEqual(test_case.template, "Test Case Template")
        self.assertEqual(test_case.type, TestCaseType.FUNCTIONAL)
        self.assertEqual(test_case.priority, TestCasePriority.MUST_TEST_HIGH)
        self.assertEqual(test_case.estimate, "30m")
        self.assertEqual(test_case.references, "JIRA-123")
        self.assertEqual(test_case.preconditions, "User is on login page")
        self.assertEqual(
            test_case.steps, "1. Enter email\n2. Enter password\n3. Click login"
        )
        self.assertEqual(test_case.expected, "User is logged in successfully")

    def test_should_use_default_values_for_optional_fields(self) -> None:
        """Verify defaults: type=Functional, priority=5-Must Test, is_converted=False, template='Test Case'."""
        test_case = TestCaseModel.objects.create(
            project=self.project,
            title="Test with defaults",
        )
        self.assertEqual(test_case.type, TestCaseType.FUNCTIONAL)
        self.assertEqual(test_case.priority, TestCasePriority.MUST_TEST_HIGH)
        self.assertFalse(test_case.is_converted)
        self.assertEqual(test_case.template, "Test Case")
        self.assertEqual(test_case.testrail_id, "")
        self.assertEqual(test_case.estimate, "")
        self.assertEqual(test_case.references, "")
        self.assertEqual(test_case.preconditions, "")
        self.assertEqual(test_case.steps, "")
        self.assertEqual(test_case.expected, "")

    def test_should_return_title_in_str_representation(self) -> None:
        """Verify __str__ returns the test case title."""
        test_case = TestCaseModel.objects.create(
            project=self.project,
            title="My Test Case Title",
        )
        self.assertEqual(str(test_case), "My Test Case Title")

    def test_should_cascade_delete_when_project_deleted(self) -> None:
        """Verify test cases are deleted when parent project is deleted."""
        test_case1 = TestCaseModel.objects.create(
            project=self.project,
            title="Test Case 1",
        )
        test_case2 = TestCaseModel.objects.create(
            project=self.project,
            title="Test Case 2",
        )
        test_case1_id = test_case1.id
        test_case2_id = test_case2.id

        self.project.delete()

        self.assertFalse(TestCaseModel.objects.filter(id=test_case1_id).exists())
        self.assertFalse(TestCaseModel.objects.filter(id=test_case2_id).exists())


# ============================================================================
# TEST CASE SERVICE TESTS
# ============================================================================


class TestCaseServiceTests(TestCase):
    """Tests for TestCase service layer - business logic."""

    def setUp(self) -> None:
        self.user = CustomUser.objects.create_user(
            email="testuser@example.com",
            password="testpass123",
        )
        self.other_user = CustomUser.objects.create_user(
            email="otheruser@example.com",
            password="testpass123",
        )
        self.project = create_project(user=self.user, name="Test Project", tag_names=[])
        self.other_project = create_project(
            user=self.other_user, name="Other Project", tag_names=[]
        )

    def test_should_create_test_case_with_minimal_fields(self) -> None:
        """Verify create_test_case works with only required fields."""
        test_case = create_test_case(
            project=self.project,
            data=TestCaseData(title="Minimal Test Case"),
        )
        self.assertEqual(test_case.project, self.project)
        self.assertEqual(test_case.title, "Minimal Test Case")
        self.assertIsNotNone(test_case.id)

    def test_should_create_test_case_with_all_fields(self) -> None:
        """Verify create_test_case properly saves all provided fields."""
        test_case = create_test_case(
            project=self.project,
            data=TestCaseData(
                title="Comprehensive Test",
                testrail_id="C99999",
                template="Custom Template",
                type=TestCaseType.SECURITY,
                priority=TestCasePriority.MUST_TEST,
                estimate="2h",
                references="JIRA-999",
                preconditions="System is configured",
                steps="Step 1\nStep 2\nStep 3",
                expected="Expected outcome here",
            ),
        )
        self.assertEqual(test_case.testrail_id, "C99999")
        self.assertEqual(test_case.title, "Comprehensive Test")
        self.assertEqual(test_case.template, "Custom Template")
        self.assertEqual(test_case.type, TestCaseType.SECURITY)
        self.assertEqual(test_case.priority, TestCasePriority.MUST_TEST)
        self.assertEqual(test_case.estimate, "2h")
        self.assertEqual(test_case.references, "JIRA-999")
        self.assertEqual(test_case.preconditions, "System is configured")
        self.assertEqual(test_case.steps, "Step 1\nStep 2\nStep 3")
        self.assertEqual(test_case.expected, "Expected outcome here")

    def test_should_use_default_values_when_creating(self) -> None:
        """Verify defaults are applied when optional parameters omitted."""
        test_case = create_test_case(
            project=self.project,
            data=TestCaseData(title="Test with defaults"),
        )
        self.assertEqual(test_case.testrail_id, "")
        self.assertEqual(test_case.template, "Test Case")
        self.assertEqual(test_case.type, "")
        self.assertEqual(test_case.priority, "")
        self.assertEqual(test_case.estimate, "")
        self.assertEqual(test_case.references, "")
        self.assertEqual(test_case.preconditions, "")
        self.assertEqual(test_case.steps, "")
        self.assertEqual(test_case.expected, "")

    def test_should_update_test_case_with_all_fields(self) -> None:
        """Verify update_test_case modifies all fields correctly."""
        test_case = create_test_case(
            project=self.project,
            data=TestCaseData(title="Original Title"),
        )
        updated = update_test_case(
            test_case=test_case,
            data=TestCaseData(
                title="Updated Title",
                testrail_id="C00001",
                template="Updated Template",
                type=TestCaseType.PERFORMANCE,
                priority=TestCasePriority.TEST_IF_TIME_LOW,
                estimate="1h",
                references="JIRA-111",
                preconditions="Updated preconditions",
                steps="Updated steps",
                expected="Updated expected",
            ),
        )
        self.assertEqual(updated.title, "Updated Title")
        self.assertEqual(updated.testrail_id, "C00001")
        self.assertEqual(updated.template, "Updated Template")
        self.assertEqual(updated.type, TestCaseType.PERFORMANCE)
        self.assertEqual(updated.priority, TestCasePriority.TEST_IF_TIME_LOW)
        self.assertEqual(updated.estimate, "1h")
        self.assertEqual(updated.references, "JIRA-111")
        self.assertEqual(updated.preconditions, "Updated preconditions")
        self.assertEqual(updated.steps, "Updated steps")
        self.assertEqual(updated.expected, "Updated expected")

        # Verify it was actually saved to DB
        test_case.refresh_from_db()
        self.assertEqual(test_case.title, "Updated Title")

    def test_should_delete_test_case(self) -> None:
        """Verify delete_test_case removes test case from database."""
        test_case = create_test_case(
            project=self.project,
            data=TestCaseData(title="To be deleted"),
        )
        test_case_id = test_case.id

        delete_test_case(test_case)

        self.assertFalse(TestCaseModel.objects.filter(id=test_case_id).exists())

    def test_should_get_test_case_for_project_when_valid(self) -> None:
        """Verify get_test_case_for_project returns correct test case."""
        test_case = create_test_case(
            project=self.project,
            data=TestCaseData(title="Valid Test Case"),
        )
        retrieved = get_test_case_for_project(test_case.id, self.project)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.id, test_case.id)  # type: ignore[union-attr]
        self.assertEqual(retrieved.title, "Valid Test Case")  # type: ignore[union-attr]

    def test_should_return_none_when_test_case_in_different_project(self) -> None:
        """Verify service enforces project boundaries - cannot access other project's test cases."""
        test_case = create_test_case(
            project=self.project,
            data=TestCaseData(title="Project 1 Test Case"),
        )
        # Try to get test case using wrong project
        retrieved = get_test_case_for_project(test_case.id, self.other_project)
        self.assertIsNone(retrieved)

    def test_should_return_none_when_test_case_does_not_exist(self) -> None:
        """Verify get_test_case_for_project handles non-existent IDs gracefully."""
        retrieved = get_test_case_for_project(99999, self.project)
        self.assertIsNone(retrieved)

    def test_should_list_test_cases_for_project(self) -> None:
        """Verify list_test_cases_for_project returns paginated results."""
        create_test_case(project=self.project, data=TestCaseData(title="Test Case 1"))
        create_test_case(project=self.project, data=TestCaseData(title="Test Case 2"))
        create_test_case(project=self.project, data=TestCaseData(title="Test Case 3"))

        page = list_test_cases_for_project(
            project=self.project,
            search=None,
            page=1,
            per_page=10,
        )
        self.assertEqual(len(page.object_list), 3)

    def test_should_filter_test_cases_by_search(self) -> None:
        """Verify list_test_cases_for_project filters by title search."""
        create_test_case(project=self.project, data=TestCaseData(title="Login Test"))
        create_test_case(project=self.project, data=TestCaseData(title="Logout Test"))
        create_test_case(
            project=self.project, data=TestCaseData(title="Profile Update")
        )

        page = list_test_cases_for_project(
            project=self.project,
            search="login",
            page=1,
            per_page=10,
        )
        self.assertEqual(len(page.object_list), 1)
        self.assertEqual(page.object_list[0].title, "Login Test")

    def test_should_return_empty_page_when_no_test_cases(self) -> None:
        """Verify list returns empty page when no test cases exist."""
        page = list_test_cases_for_project(
            project=self.project,
            search=None,
            page=1,
            per_page=10,
        )
        self.assertEqual(len(page.object_list), 0)

    def test_should_paginate_test_cases_correctly(self) -> None:
        """Verify pagination works correctly with boundary values."""
        # Create 25 test cases
        for i in range(25):
            create_test_case(
                project=self.project, data=TestCaseData(title=f"Test Case {i}")
            )

        # Get first page (10 items)
        page1 = list_test_cases_for_project(
            project=self.project,
            search=None,
            page=1,
            per_page=10,
        )
        self.assertEqual(len(page1.object_list), 10)
        self.assertTrue(page1.has_next())
        self.assertFalse(page1.has_previous())

        # Get last page
        page3 = list_test_cases_for_project(
            project=self.project,
            search=None,
            page=3,
            per_page=10,
        )
        self.assertEqual(len(page3.object_list), 5)
        self.assertFalse(page3.has_next())
        self.assertTrue(page3.has_previous())


# ============================================================================
# TEST CASE FORM TESTS
# ============================================================================


class TestCaseFormTests(TestCase):
    """Tests for TestCaseForm validation and behavior."""

    def test_should_be_valid_with_required_fields(self) -> None:
        """Verify form is valid with only title provided."""
        form = TestCaseForm(
            data={
                "title": "Valid Test Case",
                "type": TestCaseType.FUNCTIONAL,
                "priority": TestCasePriority.MUST_TEST_HIGH,
            }
        )
        self.assertTrue(form.is_valid())

    def test_should_be_valid_with_all_fields(self) -> None:
        """Verify form accepts all fields when provided."""
        form = TestCaseForm(
            data={
                "title": "Complete Test Case",
                "testrail_id": "C12345",
                "template": "Custom Template",
                "type": TestCaseType.SECURITY,
                "priority": TestCasePriority.MUST_TEST,
                "estimate": "1h",
                "references": "JIRA-123",
                "preconditions": "User is logged in",
                "steps": "Step 1\nStep 2",
                "expected": "Success",
            }
        )
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["title"], "Complete Test Case")
        self.assertEqual(form.cleaned_data["testrail_id"], "C12345")
        self.assertEqual(form.cleaned_data["template"], "Custom Template")

    def test_should_require_title(self) -> None:
        """Verify title field is required."""
        form = TestCaseForm(
            data={
                "title": "",
                "type": TestCaseType.FUNCTIONAL,
                "priority": TestCasePriority.MUST_TEST_HIGH,
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("title", form.errors)

    def test_should_allow_blank_optional_fields(self) -> None:
        """Verify optional fields can be left blank."""
        form = TestCaseForm(
            data={
                "title": "Test with minimal data",
                "testrail_id": "",
                "template": "",
                "type": TestCaseType.FUNCTIONAL,
                "priority": TestCasePriority.MUST_TEST_HIGH,
                "estimate": "",
                "references": "",
                "preconditions": "",
                "steps": "",
                "expected": "",
            }
        )
        self.assertTrue(form.is_valid())

    def test_should_have_correct_type_choices(self) -> None:
        """Verify type field has all TestCaseType choices."""
        form = TestCaseForm()
        type_choices = dict(form.fields["type"].choices)  # type: ignore[attr-defined]
        self.assertIn(TestCaseType.FUNCTIONAL, type_choices)
        self.assertIn(TestCaseType.SECURITY, type_choices)
        self.assertIn(TestCaseType.PERFORMANCE, type_choices)
        self.assertIn(TestCaseType.REGRESSION, type_choices)
        self.assertIn(TestCaseType.ACCEPTANCE, type_choices)

    def test_should_have_correct_priority_choices(self) -> None:
        """Verify priority field has all TestCasePriority choices."""
        form = TestCaseForm()
        priority_choices = dict(form.fields["priority"].choices)  # type: ignore[attr-defined]
        self.assertIn(TestCasePriority.DONT_TEST, priority_choices)
        self.assertIn(TestCasePriority.TEST_IF_TIME_LOW, priority_choices)
        self.assertIn(TestCasePriority.TEST_IF_TIME_MID, priority_choices)
        self.assertIn(TestCasePriority.MUST_TEST, priority_choices)
        self.assertIn(TestCasePriority.MUST_TEST_HIGH, priority_choices)

    def test_should_use_default_values(self) -> None:
        """Verify form has correct initial values for type and priority."""
        form = TestCaseForm()
        self.assertEqual(form.fields["type"].initial, TestCaseType.FUNCTIONAL)
        self.assertEqual(
            form.fields["priority"].initial, TestCasePriority.MUST_TEST_HIGH
        )
        self.assertEqual(form.fields["template"].initial, "Test Case")


# ============================================================================
# TEST CASE VIEW TESTS
# ============================================================================


class TestCaseViewTests(TestCase):
    """Tests for TestCase views - authentication, authorization, and behavior."""

    def setUp(self) -> None:
        self.user = CustomUser.objects.create_user(
            email="member@example.com",
            password="testpass123",
        )
        self.non_member = CustomUser.objects.create_user(
            email="nonmember@example.com",
            password="testpass123",
        )
        self.project = create_project(user=self.user, name="Test Project", tag_names=[])

    def test_should_require_login_for_test_case_list(self) -> None:
        """Verify test_case_list redirects to login when not authenticated."""
        url = reverse("projects:test_case_list", args=[self.project.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)  # type: ignore[attr-defined]

    def test_should_return_404_when_user_not_project_member(self) -> None:
        """Verify non-members cannot access project's test cases."""
        self.client.force_login(self.non_member)
        url = reverse("projects:test_case_list", args=[self.project.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_should_return_200_and_correct_template_for_test_case_list(self) -> None:
        """Verify test_case_list returns success with correct template."""
        self.client.force_login(self.user)
        url = reverse("projects:test_case_list", args=[self.project.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "projects/test_cases.html")

    def test_should_pass_search_query_to_test_case_list(self) -> None:
        """Verify search parameter is passed to template context."""
        self.client.force_login(self.user)
        create_test_case(project=self.project, data=TestCaseData(title="Login Test"))
        create_test_case(project=self.project, data=TestCaseData(title="Logout Test"))

        url = reverse("projects:test_case_list", args=[self.project.id])
        response = self.client.get(url, {"search": "login"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["search"], "login")

    def test_should_include_pagination_context_in_test_case_list(self) -> None:
        """Verify test_case_list includes pagination context variables."""
        self.client.force_login(self.user)
        for i in range(25):
            create_test_case(project=self.project, data=TestCaseData(title=f"Test {i}"))

        url = reverse("projects:test_case_list", args=[self.project.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("test_cases", response.context)
        page = response.context["test_cases"]
        self.assertTrue(page.has_next())

    def test_should_require_login_for_test_case_create(self) -> None:
        """Verify test_case_create redirects to login when not authenticated."""
        url = reverse("projects:test_case_create", args=[self.project.id])
        response = self.client.post(url, {"title": "New Test"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)  # type: ignore[attr-defined]

    def test_should_require_membership_for_test_case_create(self) -> None:
        """Verify non-members cannot create test cases."""
        self.client.force_login(self.non_member)
        url = reverse("projects:test_case_create", args=[self.project.id])
        response = self.client.post(
            url,
            {
                "title": "Unauthorized Test",
                "type": TestCaseType.FUNCTIONAL,
                "priority": TestCasePriority.MUST_TEST_HIGH,
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_should_create_test_case_and_redirect(self) -> None:
        """Verify POST to test_case_create creates test case and redirects."""
        self.client.force_login(self.user)
        url = reverse("projects:test_case_create", args=[self.project.id])
        response = self.client.post(
            url,
            {
                "title": "New Test Case",
                "testrail_id": "C123",
                "template": "Test Case",
                "type": TestCaseType.FUNCTIONAL,
                "priority": TestCasePriority.MUST_TEST_HIGH,
                "estimate": "30m",
                "references": "JIRA-1",
                "preconditions": "Setup done",
                "steps": "Step 1",
                "expected": "Success",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,  # type: ignore[attr-defined]
            reverse("projects:test_case_list", args=[self.project.id]),
        )

        # Verify test case was created
        test_case = TestCaseModel.objects.get(title="New Test Case")
        self.assertEqual(test_case.project, self.project)
        self.assertEqual(test_case.testrail_id, "C123")

    def test_should_return_405_for_get_request_to_test_case_create(self) -> None:
        """Verify test_case_create only accepts POST requests."""
        self.client.force_login(self.user)
        url = reverse("projects:test_case_create", args=[self.project.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 405)

    def test_should_require_login_for_test_case_edit(self) -> None:
        """Verify test_case_edit redirects to login when not authenticated."""
        test_case = create_test_case(
            project=self.project, data=TestCaseData(title="Original")
        )
        url = reverse("projects:test_case_edit", args=[self.project.id, test_case.id])
        response = self.client.post(url, {"title": "Updated"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)  # type: ignore[attr-defined]

    def test_should_require_membership_for_test_case_edit(self) -> None:
        """Verify non-members cannot edit test cases."""
        test_case = create_test_case(
            project=self.project, data=TestCaseData(title="Original")
        )
        self.client.force_login(self.non_member)
        url = reverse("projects:test_case_edit", args=[self.project.id, test_case.id])
        response = self.client.post(
            url,
            {
                "title": "Unauthorized Edit",
                "type": TestCaseType.FUNCTIONAL,
                "priority": TestCasePriority.MUST_TEST_HIGH,
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_should_update_test_case_and_redirect(self) -> None:
        """Verify POST to test_case_edit updates test case and redirects."""
        test_case = create_test_case(
            project=self.project, data=TestCaseData(title="Original Title")
        )
        self.client.force_login(self.user)
        url = reverse("projects:test_case_edit", args=[self.project.id, test_case.id])
        response = self.client.post(
            url,
            {
                "title": "Updated Title",
                "testrail_id": "C999",
                "template": "Test Case",
                "type": TestCaseType.SECURITY,
                "priority": TestCasePriority.MUST_TEST,
                "estimate": "1h",
                "references": "JIRA-999",
                "preconditions": "Updated preconditions",
                "steps": "Updated steps",
                "expected": "Updated expected",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,  # type: ignore[attr-defined]
            reverse("projects:test_case_list", args=[self.project.id]),
        )

        test_case.refresh_from_db()
        self.assertEqual(test_case.title, "Updated Title")
        self.assertEqual(test_case.testrail_id, "C999")

    def test_should_return_404_when_editing_test_case_from_different_project(
        self,
    ) -> None:
        """Verify cross-project test case access is prevented."""
        other_user = CustomUser.objects.create_user(
            email="other@example.com", password="testpass123"
        )
        other_project = create_project(
            user=other_user, name="Other Project", tag_names=[]
        )
        other_test_case = create_test_case(
            project=other_project, data=TestCaseData(title="Other Test")
        )

        self.client.force_login(self.user)
        # Try to edit other_test_case using self.project
        url = reverse(
            "projects:test_case_edit", args=[self.project.id, other_test_case.id]
        )
        response = self.client.post(
            url,
            {
                "title": "Sneaky Edit",
                "type": TestCaseType.FUNCTIONAL,
                "priority": TestCasePriority.MUST_TEST_HIGH,
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_should_return_404_when_editing_nonexistent_test_case(self) -> None:
        """Verify editing non-existent test case returns 404."""
        self.client.force_login(self.user)
        url = reverse("projects:test_case_edit", args=[self.project.id, 99999])
        response = self.client.post(
            url,
            {
                "title": "Ghost Test",
                "type": TestCaseType.FUNCTIONAL,
                "priority": TestCasePriority.MUST_TEST_HIGH,
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_should_require_login_for_test_case_delete(self) -> None:
        """Verify test_case_delete redirects to login when not authenticated."""
        test_case = create_test_case(
            project=self.project, data=TestCaseData(title="To Delete")
        )
        url = reverse("projects:test_case_delete", args=[self.project.id, test_case.id])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)  # type: ignore[attr-defined]

    def test_should_require_membership_for_test_case_delete(self) -> None:
        """Verify non-members cannot delete test cases."""
        test_case = create_test_case(
            project=self.project, data=TestCaseData(title="Protected")
        )
        self.client.force_login(self.non_member)
        url = reverse("projects:test_case_delete", args=[self.project.id, test_case.id])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 404)

    def test_should_delete_test_case_and_redirect(self) -> None:
        """Verify POST to test_case_delete removes test case and redirects."""
        test_case = create_test_case(
            project=self.project, data=TestCaseData(title="To Delete")
        )
        test_case_id = test_case.id

        self.client.force_login(self.user)
        url = reverse("projects:test_case_delete", args=[self.project.id, test_case.id])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,  # type: ignore[attr-defined]
            reverse("projects:test_case_list", args=[self.project.id]),
        )

        self.assertFalse(TestCaseModel.objects.filter(id=test_case_id).exists())

    def test_should_return_404_when_deleting_test_case_from_different_project(
        self,
    ) -> None:
        """Verify cross-project test case deletion is prevented."""
        other_user = CustomUser.objects.create_user(
            email="other@example.com", password="testpass123"
        )
        other_project = create_project(
            user=other_user, name="Other Project", tag_names=[]
        )
        other_test_case = create_test_case(
            project=other_project, data=TestCaseData(title="Other Test")
        )

        self.client.force_login(self.user)
        url = reverse(
            "projects:test_case_delete", args=[self.project.id, other_test_case.id]
        )
        response = self.client.post(url)
        self.assertEqual(response.status_code, 404)

        # Verify test case still exists
        self.assertTrue(TestCaseModel.objects.filter(id=other_test_case.id).exists())

    def test_should_return_404_when_accessing_archived_project_test_cases(
        self,
    ) -> None:
        """Verify decorator blocks access to archived project's test cases."""
        archive_project(self.project)
        self.client.force_login(self.user)
        url = reverse("projects:test_case_list", args=[self.project.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)


# ============================================================================
# TEST CASE ADMIN TESTS
# ============================================================================


class TestCaseAdminTests(TestCase):
    """Tests for TestCase admin configuration."""

    def setUp(self) -> None:
        self.site = AdminSite()
        self.admin = TestCaseAdmin(TestCaseModel, self.site)
        self.user = CustomUser.objects.create_user(
            email="admin@example.com",
            password="testpass123",
        )
        self.project = create_project(user=self.user, name="Test Project", tag_names=[])

    def test_should_be_registered_in_admin(self) -> None:
        """Verify TestCase model is registered in admin site."""
        from django.contrib import admin

        self.assertIn(TestCaseModel, admin.site._registry)

    def test_should_have_correct_list_display(self) -> None:
        """Verify list_display shows correct fields."""
        expected_fields = (
            "title",
            "project",
            "type",
            "priority",
            "is_converted",
            "created_at",
        )
        self.assertEqual(self.admin.list_display, expected_fields)

    def test_should_have_correct_list_filters(self) -> None:
        """Verify list_filter has correct filter options."""
        expected_filters = ("type", "priority", "is_converted", "project")
        self.assertEqual(self.admin.list_filter, expected_filters)

    def test_should_have_correct_search_fields(self) -> None:
        """Verify search_fields includes title and testrail_id."""
        expected_search = ("title", "testrail_id")
        self.assertEqual(self.admin.search_fields, expected_search)


# ============================================================================
# UPLOAD TEST CASES - TDD TESTS
# ============================================================================
# These tests are written TDD-style: they WILL FAIL until the Upload feature
# is implemented. Every test uses lazy imports so existing tests keep running.
# Once implemented, move the imports to module level.
# ============================================================================

import os
import tempfile
from unittest.mock import MagicMock, call, patch

from django.core.files.uploadedfile import SimpleUploadedFile

# Path to the example XML shipped with spec 004
EXAMPLE_XML_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "docs",
    "specs",
    "004.Project Test Cases",
    "test cases example.xml",
)


# ============================================================================
# UPLOAD MODEL TESTS
# ============================================================================


class TestCaseUploadModelTests(TestCase):
    """Tests for TestCaseUpload model basic functionality."""

    def setUp(self) -> None:
        from projects.models import TestCaseUpload, UploadStatus

        self.TestCaseUpload = TestCaseUpload
        self.UploadStatus = UploadStatus
        self.user = CustomUser.objects.create_user(
            email="uploader@example.com",
            password="testpass123",
        )
        self.project = create_project(
            user=self.user, name="Upload Project", tag_names=[]
        )

    def test_should_create_upload_with_all_fields(self) -> None:
        """Verify TestCaseUpload can be created with all fields populated."""
        dummy_file = SimpleUploadedFile(
            "test.xml", b"<suite></suite>", content_type="text/xml"
        )
        upload = self.TestCaseUpload.objects.create(
            project=self.project,
            uploaded_by=self.user,
            original_filename="test.xml",
            file=dummy_file,
            status=self.UploadStatus.PROCESSING,
            celery_task_id="abc-123",
            total_cases=50,
            processed_cases=10,
            error_message="partial error",
        )
        self.assertEqual(upload.project, self.project)
        self.assertEqual(upload.uploaded_by, self.user)
        self.assertEqual(upload.original_filename, "test.xml")
        self.assertTrue(upload.file)
        self.assertEqual(upload.status, self.UploadStatus.PROCESSING)
        self.assertEqual(upload.celery_task_id, "abc-123")
        self.assertEqual(upload.total_cases, 50)
        self.assertEqual(upload.processed_cases, 10)
        self.assertEqual(upload.error_message, "partial error")

    def test_should_default_status_to_pending(self) -> None:
        """Verify default status is 'pending' when not explicitly set."""
        dummy_file = SimpleUploadedFile(
            "default.xml", b"<suite></suite>", content_type="text/xml"
        )
        upload = self.TestCaseUpload.objects.create(
            project=self.project,
            uploaded_by=self.user,
            original_filename="default.xml",
            file=dummy_file,
        )
        self.assertEqual(upload.status, self.UploadStatus.PENDING)

    def test_should_auto_set_timestamps(self) -> None:
        """Verify created_at and updated_at are auto-populated."""
        dummy_file = SimpleUploadedFile(
            "ts.xml", b"<suite></suite>", content_type="text/xml"
        )
        upload = self.TestCaseUpload.objects.create(
            project=self.project,
            uploaded_by=self.user,
            original_filename="ts.xml",
            file=dummy_file,
        )
        self.assertIsNotNone(upload.created_at)
        self.assertIsNotNone(upload.updated_at)

    def test_should_return_original_filename_in_str(self) -> None:
        """Verify __str__ returns original_filename."""
        dummy_file = SimpleUploadedFile(
            "my_export.xml", b"<suite></suite>", content_type="text/xml"
        )
        upload = self.TestCaseUpload.objects.create(
            project=self.project,
            uploaded_by=self.user,
            original_filename="my_export.xml",
            file=dummy_file,
        )
        self.assertEqual(str(upload), "my_export.xml")

    def test_should_cascade_delete_when_project_deleted(self) -> None:
        """Verify uploads are deleted when parent project is deleted."""
        dummy_file = SimpleUploadedFile(
            "cascade.xml", b"<suite></suite>", content_type="text/xml"
        )
        upload = self.TestCaseUpload.objects.create(
            project=self.project,
            uploaded_by=self.user,
            original_filename="cascade.xml",
            file=dummy_file,
        )
        upload_id = upload.id
        self.project.delete()
        self.assertFalse(self.TestCaseUpload.objects.filter(id=upload_id).exists())

    def test_should_cascade_delete_test_cases_via_upload(self) -> None:
        """Verify test cases linked to upload are deleted when upload is deleted."""
        dummy_file = SimpleUploadedFile(
            "cascade_tc.xml", b"<suite></suite>", content_type="text/xml"
        )
        upload = self.TestCaseUpload.objects.create(
            project=self.project,
            uploaded_by=self.user,
            original_filename="cascade_tc.xml",
            file=dummy_file,
        )
        tc1 = TestCaseModel.objects.create(
            project=self.project,
            title="Upload TC 1",
            upload=upload,
        )
        tc2 = TestCaseModel.objects.create(
            project=self.project,
            title="Upload TC 2",
            upload=upload,
        )
        tc1_id = tc1.id
        tc2_id = tc2.id

        upload.delete()

        self.assertFalse(TestCaseModel.objects.filter(id=tc1_id).exists())
        self.assertFalse(TestCaseModel.objects.filter(id=tc2_id).exists())

    def test_should_allow_test_case_without_upload(self) -> None:
        """Verify test cases can still exist without an upload FK (backward compat)."""
        tc = TestCaseModel.objects.create(
            project=self.project,
            title="Manual TC",
        )
        self.assertIsNone(tc.upload)
        self.assertIsNotNone(tc.id)


# ============================================================================
# UPLOAD SERVICE TESTS
# ============================================================================


class CreateUploadServiceTests(TestCase):
    """Tests for create_upload service function."""

    def setUp(self) -> None:
        from projects.models import UploadStatus
        from projects.services import create_upload

        self.UploadStatus = UploadStatus
        self.create_upload = create_upload
        self.user = CustomUser.objects.create_user(
            email="uploader@example.com",
            password="testpass123",
        )
        self.project = create_project(
            user=self.user, name="Upload Project", tag_names=[]
        )

    def test_should_create_upload_record_with_file(self) -> None:
        """Verify create_upload persists a TestCaseUpload with the provided file."""
        xml_file = SimpleUploadedFile(
            "export.xml", b"<suite><cases></cases></suite>", content_type="text/xml"
        )
        upload = self.create_upload(
            project=self.project,
            user=self.user,
            file=xml_file,
        )
        self.assertIsNotNone(upload.id)
        self.assertEqual(upload.project, self.project)
        self.assertEqual(upload.uploaded_by, self.user)
        self.assertEqual(upload.original_filename, "export.xml")
        self.assertEqual(upload.status, self.UploadStatus.PENDING)
        self.assertTrue(upload.file)

    def test_should_preserve_original_filename(self) -> None:
        """Verify original_filename stores the user's filename, not the storage path."""
        xml_file = SimpleUploadedFile(
            "My TestRail Export (2).xml",
            b"<suite></suite>",
            content_type="text/xml",
        )
        upload = self.create_upload(
            project=self.project,
            user=self.user,
            file=xml_file,
        )
        self.assertEqual(upload.original_filename, "My TestRail Export (2).xml")


class StartUploadProcessingServiceTests(TestCase):
    """Tests for start_upload_processing service function."""

    def setUp(self) -> None:
        from projects.models import TestCaseUpload, UploadStatus
        from projects.services import start_upload_processing

        self.TestCaseUpload = TestCaseUpload
        self.UploadStatus = UploadStatus
        self.start_upload_processing = start_upload_processing
        self.user = CustomUser.objects.create_user(
            email="processor@example.com",
            password="testpass123",
        )
        self.project = create_project(
            user=self.user, name="Processing Project", tag_names=[]
        )
        dummy_file = SimpleUploadedFile(
            "start.xml", b"<suite></suite>", content_type="text/xml"
        )
        self.upload = self.TestCaseUpload.objects.create(
            project=self.project,
            uploaded_by=self.user,
            original_filename="start.xml",
            file=dummy_file,
        )

    @patch("projects.tasks.process_xml_upload.delay")
    def test_should_dispatch_celery_task_and_save_task_id(
        self, mock_delay: MagicMock
    ) -> None:
        """Verify start_upload_processing dispatches Celery task and persists task_id."""
        mock_result = MagicMock()
        mock_result.id = "celery-task-uuid-123"
        mock_delay.return_value = mock_result

        self.start_upload_processing(self.upload)

        mock_delay.assert_called_once_with(self.upload.id)
        self.upload.refresh_from_db()
        self.assertEqual(self.upload.celery_task_id, "celery-task-uuid-123")
        self.assertEqual(self.upload.status, self.UploadStatus.PROCESSING)


class CancelUploadProcessingServiceTests(TestCase):
    """Tests for cancel_upload_processing service function."""

    def setUp(self) -> None:
        from projects.models import TestCaseUpload, UploadStatus
        from projects.services import cancel_upload_processing

        self.TestCaseUpload = TestCaseUpload
        self.UploadStatus = UploadStatus
        self.cancel_upload_processing = cancel_upload_processing
        self.user = CustomUser.objects.create_user(
            email="canceller@example.com",
            password="testpass123",
        )
        self.project = create_project(
            user=self.user, name="Cancel Project", tag_names=[]
        )
        dummy_file = SimpleUploadedFile(
            "cancel.xml", b"<suite></suite>", content_type="text/xml"
        )
        self.upload = self.TestCaseUpload.objects.create(
            project=self.project,
            uploaded_by=self.user,
            original_filename="cancel.xml",
            file=dummy_file,
            status=self.UploadStatus.PROCESSING,
            celery_task_id="task-to-cancel-999",
        )
        self.tc1 = TestCaseModel.objects.create(
            project=self.project,
            title="Partial TC 1",
            upload=self.upload,
        )
        self.tc2 = TestCaseModel.objects.create(
            project=self.project,
            title="Partial TC 2",
            upload=self.upload,
        )

    @patch("auto_tester.celery.app.control.revoke")
    def test_should_revoke_celery_task(self, mock_revoke: MagicMock) -> None:
        """Verify cancel revokes the Celery task with terminate=True."""
        self.cancel_upload_processing(self.upload)
        mock_revoke.assert_called_once_with("task-to-cancel-999", terminate=True)

    @patch("auto_tester.celery.app.control.revoke")
    def test_should_delete_partial_test_cases(self, mock_revoke: MagicMock) -> None:
        """Verify cancel deletes test cases that were partially imported."""
        tc1_id = self.tc1.id
        tc2_id = self.tc2.id

        self.cancel_upload_processing(self.upload)

        self.assertFalse(TestCaseModel.objects.filter(id=tc1_id).exists())
        self.assertFalse(TestCaseModel.objects.filter(id=tc2_id).exists())

    @patch("auto_tester.celery.app.control.revoke")
    def test_should_set_status_to_cancelled(self, mock_revoke: MagicMock) -> None:
        """Verify cancel sets the upload status to CANCELLED."""
        self.cancel_upload_processing(self.upload)
        self.upload.refresh_from_db()
        self.assertEqual(self.upload.status, self.UploadStatus.CANCELLED)


class DeleteUploadServiceTests(TestCase):
    """Tests for delete_upload service function."""

    def setUp(self) -> None:
        from projects.models import TestCaseUpload, UploadStatus
        from projects.services import delete_upload

        self.TestCaseUpload = TestCaseUpload
        self.UploadStatus = UploadStatus
        self.delete_upload = delete_upload
        self.user = CustomUser.objects.create_user(
            email="deleter@example.com",
            password="testpass123",
        )
        self.project = create_project(
            user=self.user, name="Delete Project", tag_names=[]
        )
        dummy_file = SimpleUploadedFile(
            "delete.xml", b"<suite></suite>", content_type="text/xml"
        )
        self.upload = self.TestCaseUpload.objects.create(
            project=self.project,
            uploaded_by=self.user,
            original_filename="delete.xml",
            file=dummy_file,
            status=self.UploadStatus.COMPLETED,
        )
        self.tc = TestCaseModel.objects.create(
            project=self.project,
            title="Linked TC",
            upload=self.upload,
        )

    def test_should_delete_upload_and_cascade_test_cases(self) -> None:
        """Verify delete_upload removes the upload and cascades to test cases."""
        upload_id = self.upload.id
        tc_id = self.tc.id

        self.delete_upload(self.upload)

        self.assertFalse(self.TestCaseUpload.objects.filter(id=upload_id).exists())
        self.assertFalse(TestCaseModel.objects.filter(id=tc_id).exists())


class GetUploadForProjectServiceTests(TestCase):
    """Tests for get_upload_for_project service function."""

    def setUp(self) -> None:
        from projects.models import TestCaseUpload
        from projects.services import get_upload_for_project

        self.TestCaseUpload = TestCaseUpload
        self.get_upload_for_project = get_upload_for_project
        self.user = CustomUser.objects.create_user(
            email="getter@example.com",
            password="testpass123",
        )
        self.project = create_project(
            user=self.user, name="Get Upload Project", tag_names=[]
        )
        self.other_user = CustomUser.objects.create_user(
            email="other_getter@example.com",
            password="testpass123",
        )
        self.other_project = create_project(
            user=self.other_user, name="Other Project", tag_names=[]
        )
        dummy_file = SimpleUploadedFile(
            "get.xml", b"<suite></suite>", content_type="text/xml"
        )
        self.upload = self.TestCaseUpload.objects.create(
            project=self.project,
            uploaded_by=self.user,
            original_filename="get.xml",
            file=dummy_file,
        )

    def test_should_return_upload_when_valid(self) -> None:
        """Verify returns upload when it belongs to the given project."""
        result = self.get_upload_for_project(self.upload.id, self.project)
        self.assertIsNotNone(result)
        self.assertEqual(result.id, self.upload.id)  # type: ignore[union-attr]

    def test_should_return_none_when_upload_in_different_project(self) -> None:
        """Verify returns None when upload belongs to a different project."""
        result = self.get_upload_for_project(self.upload.id, self.other_project)
        self.assertIsNone(result)

    def test_should_return_none_when_upload_does_not_exist(self) -> None:
        """Verify returns None for non-existent upload ID."""
        result = self.get_upload_for_project(99999, self.project)
        self.assertIsNone(result)


class ListUploadsForProjectServiceTests(TestCase):
    """Tests for list_uploads_for_project service function."""

    def setUp(self) -> None:
        from projects.models import TestCaseUpload
        from projects.services import list_uploads_for_project

        self.TestCaseUpload = TestCaseUpload
        self.list_uploads_for_project = list_uploads_for_project
        self.user = CustomUser.objects.create_user(
            email="lister@example.com",
            password="testpass123",
        )
        self.project = create_project(
            user=self.user, name="List Upload Project", tag_names=[]
        )

    def test_should_return_uploads_for_project(self) -> None:
        """Verify returns all uploads belonging to the project."""
        for i in range(3):
            dummy_file = SimpleUploadedFile(
                f"file{i}.xml", b"<suite></suite>", content_type="text/xml"
            )
            self.TestCaseUpload.objects.create(
                project=self.project,
                uploaded_by=self.user,
                original_filename=f"file{i}.xml",
                file=dummy_file,
            )

        page = self.list_uploads_for_project(
            project=self.project,
            page=1,
            per_page=10,
        )
        self.assertEqual(len(page.object_list), 3)

    def test_should_paginate_uploads(self) -> None:
        """Verify uploads are paginated correctly."""
        for i in range(5):
            dummy_file = SimpleUploadedFile(
                f"page{i}.xml", b"<suite></suite>", content_type="text/xml"
            )
            self.TestCaseUpload.objects.create(
                project=self.project,
                uploaded_by=self.user,
                original_filename=f"page{i}.xml",
                file=dummy_file,
            )

        page1 = self.list_uploads_for_project(
            project=self.project,
            page=1,
            per_page=2,
        )
        self.assertEqual(len(page1.object_list), 2)
        self.assertTrue(page1.has_next())

        page3 = self.list_uploads_for_project(
            project=self.project,
            page=3,
            per_page=2,
        )
        self.assertEqual(len(page3.object_list), 1)
        self.assertFalse(page3.has_next())

    def test_should_exclude_uploads_from_other_projects(self) -> None:
        """Verify only uploads for the given project are returned."""
        other_user = CustomUser.objects.create_user(
            email="other_lister@example.com",
            password="testpass123",
        )
        other_project = create_project(
            user=other_user, name="Other List Project", tag_names=[]
        )
        dummy_file = SimpleUploadedFile(
            "other.xml", b"<suite></suite>", content_type="text/xml"
        )
        self.TestCaseUpload.objects.create(
            project=other_project,
            uploaded_by=other_user,
            original_filename="other.xml",
            file=dummy_file,
        )

        page = self.list_uploads_for_project(
            project=self.project,
            page=1,
            per_page=10,
        )
        self.assertEqual(len(page.object_list), 0)

    def test_should_return_empty_page_when_no_uploads(self) -> None:
        """Verify returns empty page when project has no uploads."""
        page = self.list_uploads_for_project(
            project=self.project,
            page=1,
            per_page=10,
        )
        self.assertEqual(len(page.object_list), 0)


# ============================================================================
# XML PARSING SERVICE TESTS
# ============================================================================


class ValidateTestrailXmlServiceTests(TestCase):
    """Tests for validate_testrail_xml service function."""

    def setUp(self) -> None:
        from projects.services import validate_testrail_xml

        self.validate_testrail_xml = validate_testrail_xml

    def test_should_return_true_for_valid_xml_with_suite_and_cases(self) -> None:
        """Verify valid TestRail XML with suite/sections/cases passes validation."""
        valid_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<suite>\n"
            "  <id>S1</id>\n"
            "  <name>Master</name>\n"
            "  <sections>\n"
            "    <section>\n"
            "      <name>Section 1</name>\n"
            "      <cases>\n"
            "        <case>\n"
            "          <id>C1</id>\n"
            "          <title>Test 1</title>\n"
            "        </case>\n"
            "      </cases>\n"
            "    </section>\n"
            "  </sections>\n"
            "</suite>"
        )
        is_valid, error = self.validate_testrail_xml(valid_xml)
        self.assertTrue(is_valid)
        self.assertEqual(error, "")

    def test_should_return_false_for_malformed_xml(self) -> None:
        """Verify malformed XML fails validation with error message."""
        malformed = "<suite><unclosed>"
        is_valid, error = self.validate_testrail_xml(malformed)
        self.assertFalse(is_valid)
        self.assertNotEqual(error, "")

    def test_should_return_false_for_xml_without_suite_tag(self) -> None:
        """Verify XML without <suite> root tag fails validation."""
        no_suite = (
            '<?xml version="1.0" encoding="UTF-8"?>\n' "<root><data>stuff</data></root>"
        )
        is_valid, error = self.validate_testrail_xml(no_suite)
        self.assertFalse(is_valid)
        self.assertIn("suite", error.lower())

    def test_should_return_false_for_empty_string(self) -> None:
        """Verify empty string fails validation."""
        is_valid, error = self.validate_testrail_xml("")
        self.assertFalse(is_valid)
        self.assertNotEqual(error, "")

    def test_should_return_false_for_non_xml_content(self) -> None:
        """Verify non-XML content (e.g. JSON) fails validation."""
        json_content = '{"test": "not xml"}'
        is_valid, error = self.validate_testrail_xml(json_content)
        self.assertFalse(is_valid)
        self.assertNotEqual(error, "")

    def test_should_return_false_for_xml_with_suite_but_no_cases(self) -> None:
        """Verify XML with suite tag but no case elements fails validation."""
        no_cases = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<suite>\n"
            "  <id>S1</id>\n"
            "  <name>Empty</name>\n"
            "  <sections></sections>\n"
            "</suite>"
        )
        is_valid, error = self.validate_testrail_xml(no_cases)
        self.assertFalse(is_valid)
        self.assertIn("case", error.lower())


class ParseTestrailXmlServiceTests(TestCase):
    """Tests for parse_testrail_xml service function."""

    def setUp(self) -> None:
        from projects.services import parse_testrail_xml

        self.parse_testrail_xml = parse_testrail_xml

    def test_should_parse_example_xml_and_return_parsed_cases(self) -> None:
        """Verify parse_testrail_xml returns a list of ParsedTestCase from real XML."""
        parsed = self.parse_testrail_xml(EXAMPLE_XML_PATH)
        self.assertIsInstance(parsed, list)
        self.assertGreater(len(parsed), 0)

    def test_should_extract_basic_fields_from_case(self) -> None:
        """Verify parsed cases have correct id, title, template, type, priority."""
        xml_content = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<suite>\n"
            "  <id>S1</id>\n"
            "  <name>Test</name>\n"
            "  <sections>\n"
            "    <section>\n"
            "      <name>Sec 1</name>\n"
            "      <cases>\n"
            "        <case>\n"
            "          <id>C42</id>\n"
            "          <title>Login with valid creds</title>\n"
            "          <template>Test Case</template>\n"
            "          <type>Functional</type>\n"
            "          <priority>4 - Must Test</priority>\n"
            "          <estimate>30m</estimate>\n"
            "          <references>JIRA-100</references>\n"
            "          <is_converted>0</is_converted>\n"
            "        </case>\n"
            "      </cases>\n"
            "    </section>\n"
            "  </sections>\n"
            "</suite>"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False) as f:
            f.write(xml_content)
            f.flush()
            temp_path = f.name

        try:
            parsed = self.parse_testrail_xml(temp_path)
            self.assertEqual(len(parsed), 1)
            case = parsed[0]
            self.assertEqual(case.testrail_id, "C42")
            self.assertEqual(case.title, "Login with valid creds")
            self.assertEqual(case.template, "Test Case")
            self.assertEqual(case.type, "Functional")
            self.assertEqual(case.priority, "4 - Must Test")
            self.assertEqual(case.estimate, "30m")
            self.assertEqual(case.references, "JIRA-100")
            self.assertFalse(case.is_converted)
        finally:
            os.unlink(temp_path)

    def test_should_decode_html_encoded_custom_fields(self) -> None:
        """Verify HTML-encoded preconditions, steps, expected are decoded correctly."""
        xml_content = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<suite>\n"
            "  <id>S1</id><name>T</name>\n"
            "  <sections><section><name>S</name><cases>\n"
            "    <case>\n"
            "      <id>C100</id>\n"
            "      <title>HTML encoded test</title>\n"
            "      <template>Test Case</template>\n"
            "      <type>Acceptance</type>\n"
            "      <priority>5 - Must Test</priority>\n"
            "      <estimate></estimate>\n"
            "      <references></references>\n"
            "      <custom>\n"
            "        <preconds>&lt;p&gt;Setup the environment.&lt;/p&gt;</preconds>\n"
            "        <steps>&lt;ol&gt;&lt;li&gt;Step one&lt;/li&gt;&lt;/ol&gt;</steps>\n"
            "        <expected>&lt;p&gt;All should pass.&lt;/p&gt;</expected>\n"
            "      </custom>\n"
            "      <is_converted>1</is_converted>\n"
            "    </case>\n"
            "  </cases></section></sections>\n"
            "</suite>"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False) as f:
            f.write(xml_content)
            f.flush()
            temp_path = f.name

        try:
            parsed = self.parse_testrail_xml(temp_path)
            self.assertEqual(len(parsed), 1)
            case = parsed[0]
            # The HTML entities should be decoded
            self.assertIn("<p>", case.preconditions)
            self.assertIn("Setup the environment.", case.preconditions)
            self.assertIn("<ol>", case.steps)
            self.assertIn("<p>", case.expected)
        finally:
            os.unlink(temp_path)

    def test_should_map_is_converted_one_to_true(self) -> None:
        """Verify is_converted='1' maps to True."""
        xml_content = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<suite><id>S1</id><name>T</name>\n"
            "  <sections><section><name>S</name><cases>\n"
            "    <case>\n"
            "      <id>C200</id><title>Converted</title>\n"
            "      <template>Test Case</template><type>Functional</type>\n"
            "      <priority>4 - Must Test</priority>\n"
            "      <estimate></estimate><references></references>\n"
            "      <is_converted>1</is_converted>\n"
            "    </case>\n"
            "  </cases></section></sections>\n"
            "</suite>"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False) as f:
            f.write(xml_content)
            f.flush()
            temp_path = f.name

        try:
            parsed = self.parse_testrail_xml(temp_path)
            self.assertTrue(parsed[0].is_converted)
        finally:
            os.unlink(temp_path)

    def test_should_map_is_converted_zero_to_false(self) -> None:
        """Verify is_converted='0' maps to False."""
        xml_content = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<suite><id>S1</id><name>T</name>\n"
            "  <sections><section><name>S</name><cases>\n"
            "    <case>\n"
            "      <id>C201</id><title>Not Converted</title>\n"
            "      <template>Test Case</template><type>Functional</type>\n"
            "      <priority>4 - Must Test</priority>\n"
            "      <estimate></estimate><references></references>\n"
            "      <is_converted>0</is_converted>\n"
            "    </case>\n"
            "  </cases></section></sections>\n"
            "</suite>"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False) as f:
            f.write(xml_content)
            f.flush()
            temp_path = f.name

        try:
            parsed = self.parse_testrail_xml(temp_path)
            self.assertFalse(parsed[0].is_converted)
        finally:
            os.unlink(temp_path)

    def test_should_handle_cases_with_no_custom_children(self) -> None:
        """Verify parsing works when a case has no <custom> block (empty preconditions/steps/expected)."""
        xml_content = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<suite><id>S1</id><name>T</name>\n"
            "  <sections><section><name>S</name><cases>\n"
            "    <case>\n"
            "      <id>C300</id><title>No custom</title>\n"
            "      <template>Test Case</template><type>Functional</type>\n"
            "      <priority>4 - Must Test</priority>\n"
            "      <estimate></estimate><references></references>\n"
            "      <is_converted>0</is_converted>\n"
            "    </case>\n"
            "  </cases></section></sections>\n"
            "</suite>"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False) as f:
            f.write(xml_content)
            f.flush()
            temp_path = f.name

        try:
            parsed = self.parse_testrail_xml(temp_path)
            self.assertEqual(len(parsed), 1)
            case = parsed[0]
            self.assertEqual(case.preconditions, "")
            self.assertEqual(case.steps, "")
            self.assertEqual(case.expected, "")
        finally:
            os.unlink(temp_path)

    def test_should_parse_multiple_sections_with_multiple_cases(self) -> None:
        """Verify parser handles multiple sections each with multiple cases."""
        xml_content = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<suite><id>S1</id><name>T</name>\n"
            "  <sections>\n"
            "    <section><name>S1</name><cases>\n"
            "      <case><id>C1</id><title>T1</title>"
            "<template>Test Case</template><type>Functional</type>"
            "<priority>4 - Must Test</priority>"
            "<estimate></estimate><references></references>"
            "<is_converted>0</is_converted></case>\n"
            "      <case><id>C2</id><title>T2</title>"
            "<template>Test Case</template><type>Functional</type>"
            "<priority>4 - Must Test</priority>"
            "<estimate></estimate><references></references>"
            "<is_converted>0</is_converted></case>\n"
            "    </cases></section>\n"
            "    <section><name>S2</name><cases>\n"
            "      <case><id>C3</id><title>T3</title>"
            "<template>Test Case</template><type>Functional</type>"
            "<priority>4 - Must Test</priority>"
            "<estimate></estimate><references></references>"
            "<is_converted>1</is_converted></case>\n"
            "    </cases></section>\n"
            "  </sections>\n"
            "</suite>"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False) as f:
            f.write(xml_content)
            f.flush()
            temp_path = f.name

        try:
            parsed = self.parse_testrail_xml(temp_path)
            self.assertEqual(len(parsed), 3)
            titles = [c.title for c in parsed]
            self.assertIn("T1", titles)
            self.assertIn("T2", titles)
            self.assertIn("T3", titles)
        finally:
            os.unlink(temp_path)


class BulkCreateTestCasesFromParsedServiceTests(TestCase):
    """Tests for bulk_create_test_cases_from_parsed service function."""

    def setUp(self) -> None:
        from projects.models import TestCaseUpload
        from projects.services import bulk_create_test_cases_from_parsed

        self.TestCaseUpload = TestCaseUpload
        self.bulk_create_test_cases_from_parsed = bulk_create_test_cases_from_parsed
        self.user = CustomUser.objects.create_user(
            email="bulk@example.com",
            password="testpass123",
        )
        self.project = create_project(user=self.user, name="Bulk Project", tag_names=[])
        dummy_file = SimpleUploadedFile(
            "bulk.xml", b"<suite></suite>", content_type="text/xml"
        )
        self.upload = self.TestCaseUpload.objects.create(
            project=self.project,
            uploaded_by=self.user,
            original_filename="bulk.xml",
            file=dummy_file,
        )

    def test_should_create_correct_count_of_test_cases(self) -> None:
        """Verify bulk_create creates the right number of DB records."""
        from projects.models import ParsedTestCase

        parsed_cases = [
            ParsedTestCase(
                testrail_id=f"C{i}",
                title=f"Bulk Test {i}",
                template="Test Case",
                type="Functional",
                priority="4 - Must Test",
                estimate="",
                references="",
                preconditions="",
                steps="",
                expected="",
                is_converted=False,
            )
            for i in range(10)
        ]

        self.bulk_create_test_cases_from_parsed(
            upload=self.upload,
            project=self.project,
            parsed_cases=parsed_cases,
        )

        self.assertEqual(
            TestCaseModel.objects.filter(
                project=self.project, upload=self.upload
            ).count(),
            10,
        )

    def test_should_call_progress_callback(self) -> None:
        """Verify progress_callback is called during bulk creation."""
        from projects.models import ParsedTestCase

        parsed_cases = [
            ParsedTestCase(
                testrail_id=f"C{i}",
                title=f"CB Test {i}",
                template="Test Case",
                type="Functional",
                priority="4 - Must Test",
                estimate="",
                references="",
                preconditions="",
                steps="",
                expected="",
                is_converted=False,
            )
            for i in range(5)
        ]

        callback = MagicMock()

        self.bulk_create_test_cases_from_parsed(
            upload=self.upload,
            project=self.project,
            parsed_cases=parsed_cases,
            progress_callback=callback,
        )

        # Callback should have been called at least once
        self.assertTrue(callback.called)

    def test_should_link_test_cases_to_upload(self) -> None:
        """Verify all created test cases have the upload FK set."""
        from projects.models import ParsedTestCase

        parsed_cases = [
            ParsedTestCase(
                testrail_id="C999",
                title="Linked Test",
                template="Test Case",
                type="Functional",
                priority="4 - Must Test",
                estimate="",
                references="",
                preconditions="Some precond",
                steps="Step 1",
                expected="Expected result",
                is_converted=True,
            )
        ]

        self.bulk_create_test_cases_from_parsed(
            upload=self.upload,
            project=self.project,
            parsed_cases=parsed_cases,
        )

        tc = TestCaseModel.objects.get(testrail_id="C999")
        self.assertEqual(tc.upload, self.upload)
        self.assertEqual(tc.project, self.project)
        self.assertEqual(tc.preconditions, "Some precond")
        self.assertEqual(tc.steps, "Step 1")
        self.assertEqual(tc.expected, "Expected result")
        self.assertTrue(tc.is_converted)

    def test_should_respect_batch_size(self) -> None:
        """Verify batch_size parameter controls how many are created per batch."""
        from projects.models import ParsedTestCase

        parsed_cases = [
            ParsedTestCase(
                testrail_id=f"C{i}",
                title=f"Batch Test {i}",
                template="Test Case",
                type="Functional",
                priority="4 - Must Test",
                estimate="",
                references="",
                preconditions="",
                steps="",
                expected="",
                is_converted=False,
            )
            for i in range(7)
        ]

        callback = MagicMock()

        self.bulk_create_test_cases_from_parsed(
            upload=self.upload,
            project=self.project,
            parsed_cases=parsed_cases,
            batch_size=3,
            progress_callback=callback,
        )

        # All 7 should be created regardless of batch size
        self.assertEqual(
            TestCaseModel.objects.filter(upload=self.upload).count(),
            7,
        )
        # Progress callback should be called for each batch (3 batches: 3+3+1)
        self.assertGreaterEqual(callback.call_count, 3)


# ============================================================================
# LIST TEST CASES UPLOAD FILTER TESTS (backward-compat)
# ============================================================================


class ListTestCasesUploadFilterTests(TestCase):
    """Tests for list_test_cases_for_project with upload_id filter."""

    def setUp(self) -> None:
        self.user = CustomUser.objects.create_user(
            email="filter@example.com",
            password="testpass123",
        )
        self.project = create_project(
            user=self.user, name="Filter Project", tag_names=[]
        )
        # Create an upload with linked test cases
        dummy_file = SimpleUploadedFile(
            "filter.xml", b"<suite></suite>", content_type="text/xml"
        )
        self.upload = TestCaseUpload.objects.create(
            project=self.project,
            uploaded_by=self.user,
            original_filename="filter.xml",
            file=dummy_file,
        )
        self.tc_with_upload = TestCaseModel.objects.create(
            project=self.project,
            title="From Upload",
            upload=self.upload,
        )
        # Create a manual test case (no upload)
        self.tc_manual = create_test_case(
            project=self.project,
            data=TestCaseData(title="Manual TC"),
        )

    def test_should_filter_by_upload_id(self) -> None:
        """Verify passing upload_id returns only test cases from that upload."""
        page = list_test_cases_for_project(
            project=self.project,
            search=None,
            page=1,
            per_page=10,
            upload_id=self.upload.id,
        )
        titles = [tc.title for tc in page.object_list]
        self.assertIn("From Upload", titles)
        self.assertNotIn("Manual TC", titles)
        self.assertEqual(len(page.object_list), 1)

    def test_should_return_all_test_cases_when_no_upload_id(self) -> None:
        """Verify omitting upload_id returns all test cases (backward compatible)."""
        page = list_test_cases_for_project(
            project=self.project,
            search=None,
            page=1,
            per_page=10,
        )
        self.assertEqual(len(page.object_list), 2)


# ============================================================================
# UPLOAD VIEW TESTS
# ============================================================================


class UploadListViewTests(TestCase):
    """Tests for upload_list view."""

    def setUp(self) -> None:
        self.user = CustomUser.objects.create_user(
            email="viewlist@example.com",
            password="testpass123",
        )
        self.non_member = CustomUser.objects.create_user(
            email="nonmember_upload@example.com",
            password="testpass123",
        )
        self.project = create_project(
            user=self.user, name="View Upload Project", tag_names=[]
        )

    def test_should_redirect_when_unauthenticated(self) -> None:
        """Verify upload_list redirects to login when not authenticated."""
        url = reverse("projects:upload_list", args=[self.project.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)  # type: ignore[attr-defined]

    def test_should_return_404_when_user_not_project_member(self) -> None:
        """Verify non-members cannot access upload list."""
        self.client.force_login(self.non_member)
        url = reverse("projects:upload_list", args=[self.project.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_should_return_200_with_correct_template(self) -> None:
        """Verify upload_list returns 200 with the uploads template."""
        self.client.force_login(self.user)
        url = reverse("projects:upload_list", args=[self.project.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "projects/uploads.html")

    def test_should_include_uploads_in_context(self) -> None:
        """Verify context contains uploads for the project."""
        dummy_file = SimpleUploadedFile(
            "ctx.xml", b"<suite></suite>", content_type="text/xml"
        )
        TestCaseUpload.objects.create(
            project=self.project,
            uploaded_by=self.user,
            original_filename="ctx.xml",
            file=dummy_file,
        )
        self.client.force_login(self.user)
        url = reverse("projects:upload_list", args=[self.project.id])
        response = self.client.get(url)
        self.assertIn("uploads", response.context)


class UploadCreateViewTests(TestCase):
    """Tests for upload_create view."""

    def setUp(self) -> None:
        self.user = CustomUser.objects.create_user(
            email="viewcreate@example.com",
            password="testpass123",
        )
        self.non_member = CustomUser.objects.create_user(
            email="nonmember_create@example.com",
            password="testpass123",
        )
        self.project = create_project(
            user=self.user, name="Create Upload Project", tag_names=[]
        )

    def test_should_redirect_when_unauthenticated(self) -> None:
        """Verify upload_create redirects to login when not authenticated."""
        url = reverse("projects:upload_create", args=[self.project.id])
        xml_file = SimpleUploadedFile(
            "test.xml",
            b"<suite><sections><section><cases><case>"
            b"<id>C1</id><title>T</title></case></cases>"
            b"</section></sections></suite>",
            content_type="text/xml",
        )
        response = self.client.post(url, {"file": xml_file})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)  # type: ignore[attr-defined]

    def test_should_return_404_when_user_not_project_member(self) -> None:
        """Verify non-members cannot upload files."""
        self.client.force_login(self.non_member)
        url = reverse("projects:upload_create", args=[self.project.id])
        xml_file = SimpleUploadedFile(
            "test.xml",
            b"<suite><sections><section><cases><case>"
            b"<id>C1</id><title>T</title></case></cases>"
            b"</section></sections></suite>",
            content_type="text/xml",
        )
        response = self.client.post(url, {"file": xml_file})
        self.assertEqual(response.status_code, 404)

    @patch("projects.views.start_upload_processing")
    def test_should_create_upload_and_redirect_on_valid_xml(
        self, mock_start: MagicMock
    ) -> None:
        """Verify POST with valid XML file creates upload and redirects."""
        self.client.force_login(self.user)
        url = reverse("projects:upload_create", args=[self.project.id])
        xml_content = (
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
            b"<suite><id>S1</id><name>T</name>"
            b"<sections><section><name>S</name><cases>"
            b"<case><id>C1</id><title>Test</title>"
            b"<template>Test Case</template><type>Functional</type>"
            b"<priority>4 - Must Test</priority>"
            b"<estimate></estimate><references></references>"
            b"<is_converted>0</is_converted></case>"
            b"</cases></section></sections></suite>"
        )
        xml_file = SimpleUploadedFile("valid.xml", xml_content, content_type="text/xml")
        response = self.client.post(url, {"file": xml_file})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(TestCaseUpload.objects.filter(project=self.project).exists())

    def test_should_reject_non_xml_file(self) -> None:
        """Verify POST with non-XML file returns error (does not create upload)."""
        self.client.force_login(self.user)
        url = reverse("projects:upload_create", args=[self.project.id])
        txt_file = SimpleUploadedFile(
            "data.txt", b"plain text content", content_type="text/plain"
        )
        response = self.client.post(url, {"file": txt_file})
        # Should not create an upload
        self.assertFalse(TestCaseUpload.objects.filter(project=self.project).exists())

    def test_should_reject_invalid_xml_content(self) -> None:
        """Verify POST with .xml file but invalid XML content returns error."""
        self.client.force_login(self.user)
        url = reverse("projects:upload_create", args=[self.project.id])
        bad_xml = SimpleUploadedFile(
            "bad.xml", b"<not-a-suite>stuff</not-a-suite>", content_type="text/xml"
        )
        response = self.client.post(url, {"file": bad_xml})
        self.assertFalse(TestCaseUpload.objects.filter(project=self.project).exists())

    def test_should_return_405_for_get_request(self) -> None:
        """Verify upload_create only accepts POST requests."""
        self.client.force_login(self.user)
        url = reverse("projects:upload_create", args=[self.project.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 405)


class UploadCancelViewTests(TestCase):
    """Tests for upload_cancel view."""

    def setUp(self) -> None:
        self.user = CustomUser.objects.create_user(
            email="viewcancel@example.com",
            password="testpass123",
        )
        self.non_member = CustomUser.objects.create_user(
            email="nonmember_cancel@example.com",
            password="testpass123",
        )
        self.project = create_project(
            user=self.user, name="Cancel Upload Project", tag_names=[]
        )
        dummy_file = SimpleUploadedFile(
            "cancel_view.xml", b"<suite></suite>", content_type="text/xml"
        )
        self.upload = TestCaseUpload.objects.create(
            project=self.project,
            uploaded_by=self.user,
            original_filename="cancel_view.xml",
            file=dummy_file,
            status=UploadStatus.PROCESSING,
            celery_task_id="task-view-cancel",
        )

    def test_should_redirect_when_unauthenticated(self) -> None:
        """Verify upload_cancel redirects to login when not authenticated."""
        url = reverse("projects:upload_cancel", args=[self.project.id, self.upload.id])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)  # type: ignore[attr-defined]

    def test_should_return_404_when_user_not_project_member(self) -> None:
        """Verify non-members cannot cancel uploads."""
        self.client.force_login(self.non_member)
        url = reverse("projects:upload_cancel", args=[self.project.id, self.upload.id])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 404)

    @patch("projects.views.cancel_upload_processing")
    def test_should_cancel_processing_and_redirect(
        self, mock_cancel: MagicMock
    ) -> None:
        """Verify POST cancels upload processing and redirects."""
        self.client.force_login(self.user)
        url = reverse("projects:upload_cancel", args=[self.project.id, self.upload.id])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        mock_cancel.assert_called_once()

    def test_should_return_404_for_nonexistent_upload(self) -> None:
        """Verify cancelling non-existent upload returns 404."""
        self.client.force_login(self.user)
        url = reverse("projects:upload_cancel", args=[self.project.id, 99999])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 404)


class UploadDeleteViewTests(TestCase):
    """Tests for upload_delete view."""

    def setUp(self) -> None:
        self.user = CustomUser.objects.create_user(
            email="viewdelete@example.com",
            password="testpass123",
        )
        self.non_member = CustomUser.objects.create_user(
            email="nonmember_delete@example.com",
            password="testpass123",
        )
        self.project = create_project(
            user=self.user, name="Delete Upload Project", tag_names=[]
        )
        dummy_file = SimpleUploadedFile(
            "delete_view.xml", b"<suite></suite>", content_type="text/xml"
        )
        self.upload = TestCaseUpload.objects.create(
            project=self.project,
            uploaded_by=self.user,
            original_filename="delete_view.xml",
            file=dummy_file,
            status=UploadStatus.COMPLETED,
        )

    def test_should_redirect_when_unauthenticated(self) -> None:
        """Verify upload_delete redirects to login when not authenticated."""
        url = reverse("projects:upload_delete", args=[self.project.id, self.upload.id])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)  # type: ignore[attr-defined]

    def test_should_return_404_when_user_not_project_member(self) -> None:
        """Verify non-members cannot delete uploads."""
        self.client.force_login(self.non_member)
        url = reverse("projects:upload_delete", args=[self.project.id, self.upload.id])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 404)

    def test_should_delete_upload_and_redirect(self) -> None:
        """Verify POST deletes the upload and redirects to upload list."""
        upload_id = self.upload.id
        self.client.force_login(self.user)
        url = reverse("projects:upload_delete", args=[self.project.id, self.upload.id])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(TestCaseUpload.objects.filter(id=upload_id).exists())

    def test_should_cascade_delete_linked_test_cases(self) -> None:
        """Verify deleting upload via view cascades to its test cases."""
        tc = TestCaseModel.objects.create(
            project=self.project,
            title="Upload-linked TC",
            upload=self.upload,
        )
        tc_id = tc.id

        self.client.force_login(self.user)
        url = reverse("projects:upload_delete", args=[self.project.id, self.upload.id])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(TestCaseModel.objects.filter(id=tc_id).exists())

    def test_should_return_404_for_nonexistent_upload(self) -> None:
        """Verify deleting non-existent upload returns 404."""
        self.client.force_login(self.user)
        url = reverse("projects:upload_delete", args=[self.project.id, 99999])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 404)

    def test_should_return_404_when_accessing_archived_project_uploads(
        self,
    ) -> None:
        """Verify decorator blocks access to archived project's uploads."""
        archive_project(self.project)
        self.client.force_login(self.user)
        url = reverse("projects:upload_delete", args=[self.project.id, self.upload.id])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 404)


# ============================================================================
# UPLOAD ADMIN TESTS
# ============================================================================


class TestCaseUploadAdminTests(TestCase):
    """Tests for TestCaseUpload admin configuration."""

    def test_should_be_registered_in_admin(self) -> None:
        """Verify TestCaseUpload model is registered in admin site."""
        from django.contrib import admin

        self.assertIn(TestCaseUpload, admin.site._registry)

    def test_should_have_correct_list_display(self) -> None:
        """Verify list_display shows correct fields."""
        from projects.admin import TestCaseUploadAdmin

        site = AdminSite()
        upload_admin = TestCaseUploadAdmin(TestCaseUpload, site)
        expected_fields = (
            "original_filename",
            "project",
            "uploaded_by",
            "status",
            "total_cases",
            "processed_cases",
            "created_at",
        )
        self.assertEqual(upload_admin.list_display, expected_fields)

    def test_should_have_correct_list_filter(self) -> None:
        """Verify list_filter has correct filter options."""
        from projects.admin import TestCaseUploadAdmin

        site = AdminSite()
        upload_admin = TestCaseUploadAdmin(TestCaseUpload, site)
        expected_filters = ("status", "project")
        self.assertEqual(upload_admin.list_filter, expected_filters)

    def test_should_have_correct_search_fields(self) -> None:
        """Verify search_fields includes original_filename."""
        from projects.admin import TestCaseUploadAdmin

        site = AdminSite()
        upload_admin = TestCaseUploadAdmin(TestCaseUpload, site)
        expected_search = ("original_filename",)
        self.assertEqual(upload_admin.search_fields, expected_search)


# ============================================================================
# TEST RUN MODEL TESTS
# ============================================================================


class TestRunModelTests(TestCase):
    """Tests for TestRun model."""

    def setUp(self) -> None:
        self.user = CustomUser.objects.create_user(
            email="testrunuser@example.com", password="pass123"
        )
        self.project = Project.objects.create(name="TestRun Project")

    def test_should_create_test_run_with_defaults(self) -> None:
        run = TestRun.objects.create(project=self.project)
        self.assertEqual(run.status, TestRunStatus.WAITING)
        self.assertIsNotNone(run.created_at)
        self.assertIsNotNone(run.updated_at)

    def test_should_return_str_with_id_and_project(self) -> None:
        run = TestRun.objects.create(project=self.project)
        self.assertIn("TestRun Project", str(run))
        self.assertIn(str(run.pk), str(run))

    def test_should_cascade_delete_with_project(self) -> None:
        TestRun.objects.create(project=self.project)
        self.project.delete()
        self.assertEqual(TestRun.objects.count(), 0)


class TestRunTestCaseModelTests(TestCase):
    """Tests for TestRunTestCase pivot model."""

    def setUp(self) -> None:
        self.project = Project.objects.create(name="Pivot Project")
        self.test_case = TestCaseModel.objects.create(project=self.project, title="TC1")
        self.tr = TestRun.objects.create(project=self.project)

    def test_should_create_pivot_with_defaults(self) -> None:
        pivot = TestRunTestCase.objects.create(
            test_run=self.tr, test_case=self.test_case
        )
        self.assertEqual(pivot.status, TestRunTestCaseStatus.CREATED)
        self.assertEqual(pivot.result, "")
        self.assertEqual(pivot.logs, "")

    def test_should_enforce_unique_together(self) -> None:
        TestRunTestCase.objects.create(test_run=self.tr, test_case=self.test_case)
        with self.assertRaises(IntegrityError):
            TestRunTestCase.objects.create(test_run=self.tr, test_case=self.test_case)

    def test_should_access_test_cases_through_m2m(self) -> None:
        TestRunTestCase.objects.create(test_run=self.tr, test_case=self.test_case)
        self.assertIn(self.test_case, self.tr.test_cases.all())

    def test_should_return_str_with_id_and_title(self) -> None:
        pivot = TestRunTestCase.objects.create(
            test_run=self.tr, test_case=self.test_case
        )
        self.assertIn(str(pivot.pk), str(pivot))
        self.assertIn("TC1", str(pivot))

    def test_should_cascade_delete_with_test_run(self) -> None:
        TestRunTestCase.objects.create(test_run=self.tr, test_case=self.test_case)
        self.tr.delete()
        self.assertEqual(TestRunTestCase.objects.count(), 0)


class TestRunAdminTests(TestCase):
    """Tests for TestRun and TestRunTestCase admin registration."""

    def setUp(self) -> None:
        self.site = AdminSite()
        self.user = CustomUser.objects.create_superuser(
            email="admin@example.com", password="pass123"
        )
        self.client.login(email="admin@example.com", password="pass123")

    def test_should_register_test_run_admin(self) -> None:
        admin_instance = TestRunAdmin(TestRun, self.site)
        self.assertIn("id", admin_instance.list_display)
        self.assertIn("status", admin_instance.list_display)

    def test_should_register_test_run_test_case_admin(self) -> None:
        admin_instance = TestRunTestCaseAdmin(TestRunTestCase, self.site)
        self.assertIn("id", admin_instance.list_display)
        self.assertIn("status", admin_instance.list_display)

    def test_should_have_inline_on_test_run(self) -> None:
        admin_instance = TestRunAdmin(TestRun, self.site)
        request = RequestFactory().get("/admin/")
        request.user = self.user
        inline_classes = [type(i) for i in admin_instance.get_inline_instances(request)]
        from projects.admin import TestRunTestCaseInline

        self.assertIn(TestRunTestCaseInline, inline_classes)


# ============================================================================
# TEST RUN EXECUTION SERVICE TESTS
# ============================================================================


class FetchPivotTests(TestCase):
    def setUp(self) -> None:
        self.project = Project.objects.create(name="Fetch Pivot Proj")
        self.tc = TestCaseModel.objects.create(project=self.project, title="TC fetch")
        self.tr = TestRun.objects.create(project=self.project)
        self.pivot = TestRunTestCase.objects.create(test_run=self.tr, test_case=self.tc)

    def test_should_fetch_pivot_with_relations(self) -> None:
        fetched = _fetch_pivot(self.pivot.pk)
        self.assertEqual(fetched.pk, self.pivot.pk)
        self.assertEqual(fetched.test_case.title, "TC fetch")
        self.assertEqual(fetched.test_run.project.name, "Fetch Pivot Proj")

    def test_should_raise_on_missing_pivot(self) -> None:
        with self.assertRaises(TestRunTestCase.DoesNotExist):
            _fetch_pivot(99999)


class MarkPivotInProgressTests(TestCase):
    def setUp(self) -> None:
        self.project = Project.objects.create(name="Mark Proj")
        self.tc = TestCaseModel.objects.create(project=self.project, title="TC mark")
        self.tr = TestRun.objects.create(project=self.project)
        self.pivot = TestRunTestCase.objects.create(test_run=self.tr, test_case=self.tc)

    def test_should_set_pivot_to_in_progress(self) -> None:
        _mark_pivot_in_progress(self.pivot)
        self.pivot.refresh_from_db()
        self.assertEqual(self.pivot.status, TestRunTestCaseStatus.IN_PROGRESS)

    def test_should_set_test_run_to_started(self) -> None:
        _mark_pivot_in_progress(self.pivot)
        self.tr.refresh_from_db()
        self.assertEqual(self.tr.status, TestRunStatus.STARTED)

    def test_should_not_revert_started_status(self) -> None:
        self.tr.status = TestRunStatus.STARTED
        self.tr.save()
        _mark_pivot_in_progress(self.pivot)
        self.tr.refresh_from_db()
        self.assertEqual(self.tr.status, TestRunStatus.STARTED)


class BuildLogCallbackTests(TestCase):
    def setUp(self) -> None:
        self.project = Project.objects.create(name="Log Proj")
        self.tc = TestCaseModel.objects.create(project=self.project, title="TC log")
        self.tr = TestRun.objects.create(project=self.project)
        self.pivot = TestRunTestCase.objects.create(test_run=self.tr, test_case=self.tc)

    def test_should_append_log_to_pivot(self) -> None:
        callback = _build_log_callback(self.pivot)
        callback("line one")
        callback("line two")
        self.pivot.refresh_from_db()
        self.assertIn("line one", self.pivot.logs)
        self.assertIn("line two", self.pivot.logs)


class BuildTaskDescriptionTests(TestCase):
    def setUp(self) -> None:
        self.project = Project.objects.create(name="Desc Proj")

    def test_should_include_title(self) -> None:
        tc = TestCaseModel.objects.create(project=self.project, title="Login Test")
        desc = _build_task_description(tc)
        self.assertIn("Login Test", desc)

    def test_should_include_all_fields(self) -> None:
        tc = TestCaseModel.objects.create(
            project=self.project,
            title="Full Test",
            preconditions="User logged in",
            steps="1. Click button",
            expected="Page loads",
        )
        desc = _build_task_description(tc)
        self.assertIn("Preconditions:", desc)
        self.assertIn("User logged in", desc)
        self.assertIn("Steps:", desc)
        self.assertIn("1. Click button", desc)
        self.assertIn("Expected Result:", desc)
        self.assertIn("Page loads", desc)


class FinalizePivotTests(TestCase):
    def setUp(self) -> None:
        self.project = Project.objects.create(name="Final Proj")
        self.tc = TestCaseModel.objects.create(project=self.project, title="TC final")
        self.tr = TestRun.objects.create(project=self.project)
        self.pivot = TestRunTestCase.objects.create(test_run=self.tr, test_case=self.tc)

    def test_should_mark_success_on_task_complete(self) -> None:
        from agents.types import AgentResult, AgentStopReason, ChatMessage

        result = AgentResult(
            stop_reason=AgentStopReason.TASK_COMPLETE,
            iterations=2,
            messages=(ChatMessage(role="assistant", content="All done"),),
        )
        _finalize_pivot(self.pivot, result)
        self.pivot.refresh_from_db()
        self.assertEqual(self.pivot.status, TestRunTestCaseStatus.SUCCESS)
        self.assertEqual(self.pivot.result, "All done")

    def test_should_mark_failed_on_error(self) -> None:
        from agents.types import AgentResult, AgentStopReason

        result = AgentResult(
            stop_reason=AgentStopReason.ERROR,
            iterations=1,
            messages=(),
            error="Something broke",
        )
        _finalize_pivot(self.pivot, result)
        self.pivot.refresh_from_db()
        self.assertEqual(self.pivot.status, TestRunTestCaseStatus.FAILED)
        self.assertEqual(self.pivot.result, "Something broke")


class ExtractAgentSummaryTests(TestCase):
    def test_should_extract_last_assistant_message(self) -> None:
        from agents.types import AgentResult, AgentStopReason, ChatMessage

        result = AgentResult(
            stop_reason=AgentStopReason.TASK_COMPLETE,
            iterations=2,
            messages=(
                ChatMessage(role="assistant", content="First"),
                ChatMessage(role="tool", content="tool output", tool_call_id="t1"),
                ChatMessage(role="assistant", content="Final answer"),
            ),
        )
        self.assertEqual(_extract_agent_summary(result), "Final answer")

    def test_should_return_error_when_no_assistant(self) -> None:
        from agents.types import AgentResult, AgentStopReason

        result = AgentResult(
            stop_reason=AgentStopReason.ERROR,
            iterations=0,
            messages=(),
            error="broke",
        )
        self.assertEqual(_extract_agent_summary(result), "broke")


class MarkPivotFailedTests(TestCase):
    def setUp(self) -> None:
        self.project = Project.objects.create(name="Fail Proj")
        self.tc = TestCaseModel.objects.create(project=self.project, title="TC fail")
        self.tr = TestRun.objects.create(project=self.project)
        self.pivot = TestRunTestCase.objects.create(test_run=self.tr, test_case=self.tc)

    def test_should_mark_failed_with_error(self) -> None:
        _mark_pivot_failed(self.pivot, "oops")
        self.pivot.refresh_from_db()
        self.assertEqual(self.pivot.status, TestRunTestCaseStatus.FAILED)
        self.assertEqual(self.pivot.result, "oops")


class UpdateTestRunStatusTests(TestCase):
    def setUp(self) -> None:
        self.project = Project.objects.create(name="Status Proj")
        self.tc1 = TestCaseModel.objects.create(project=self.project, title="TC1")
        self.tc2 = TestCaseModel.objects.create(project=self.project, title="TC2")
        self.tr = TestRun.objects.create(
            project=self.project, status=TestRunStatus.STARTED
        )

    def test_should_mark_done_when_all_complete(self) -> None:
        TestRunTestCase.objects.create(
            test_run=self.tr,
            test_case=self.tc1,
            status=TestRunTestCaseStatus.SUCCESS,
        )
        TestRunTestCase.objects.create(
            test_run=self.tr,
            test_case=self.tc2,
            status=TestRunTestCaseStatus.FAILED,
        )
        _update_test_run_status_if_needed(self.tr)
        self.tr.refresh_from_db()
        self.assertEqual(self.tr.status, TestRunStatus.DONE)

    def test_should_not_mark_done_when_pending(self) -> None:
        TestRunTestCase.objects.create(
            test_run=self.tr,
            test_case=self.tc1,
            status=TestRunTestCaseStatus.SUCCESS,
        )
        TestRunTestCase.objects.create(
            test_run=self.tr,
            test_case=self.tc2,
            status=TestRunTestCaseStatus.CREATED,
        )
        _update_test_run_status_if_needed(self.tr)
        self.tr.refresh_from_db()
        self.assertEqual(self.tr.status, TestRunStatus.STARTED)


class ExecuteTestRunTestCaseIntegrationTests(TestCase):
    """Integration test for the full execute_test_run_test_case flow."""

    def setUp(self) -> None:
        self.project = Project.objects.create(name="Integration Proj")
        self.tc = TestCaseModel.objects.create(
            project=self.project,
            title="Integration TC",
            preconditions="None",
            steps="1. Open app",
            expected="App opens",
        )
        self.tr = TestRun.objects.create(project=self.project)
        self.pivot = TestRunTestCase.objects.create(test_run=self.tr, test_case=self.tc)

    @patch("projects.services.run_agent")
    @patch("projects.services.build_agent_config")
    @patch("projects.services.teardown_environment")
    @patch("projects.services.provision_environment")
    @patch("projects.services.close_docker_client")
    @patch("projects.services.get_docker_client")
    def test_should_execute_full_flow_successfully(
        self,
        mock_get_client: MagicMock,
        mock_close_client: MagicMock,
        mock_provision: MagicMock,
        mock_teardown: MagicMock,
        mock_build_config: MagicMock,
        mock_run_agent: MagicMock,
    ) -> None:
        from agents.types import (
            AgentConfig,
            AgentResult,
            AgentStopReason,
            ChatMessage,
            DMRConfig,
        )
        from environments.types import ContainerInfo, ContainerPorts

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_provision.return_value = ContainerInfo(
            container_id="abc123",
            name="test-container",
            ports=ContainerPorts(ssh=2222, vnc=5900, playwright_cdp=9222),
            status="running",
        )
        mock_build_config.return_value = AgentConfig(
            dmr=DMRConfig(host="localhost", port="12434", model="test"),
        )
        mock_run_agent.return_value = AgentResult(
            stop_reason=AgentStopReason.TASK_COMPLETE,
            iterations=3,
            messages=(ChatMessage(role="assistant", content="Test passed!"),),
        )

        execute_test_run_test_case(self.pivot.pk)

        self.pivot.refresh_from_db()
        self.assertEqual(self.pivot.status, TestRunTestCaseStatus.SUCCESS)
        self.assertEqual(self.pivot.result, "Test passed!")

        self.tr.refresh_from_db()
        self.assertEqual(self.tr.status, TestRunStatus.DONE)

        mock_teardown.assert_called_once_with(mock_client, "abc123")
        mock_close_client.assert_called_once_with(mock_client)

    @patch("projects.services.run_agent")
    @patch("projects.services.build_agent_config")
    @patch("projects.services.teardown_environment")
    @patch("projects.services.provision_environment")
    @patch("projects.services.close_docker_client")
    @patch("projects.services.get_docker_client")
    def test_should_handle_exception_gracefully(
        self,
        mock_get_client: MagicMock,
        mock_close_client: MagicMock,
        mock_provision: MagicMock,
        mock_teardown: MagicMock,
        mock_build_config: MagicMock,
        mock_run_agent: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_provision.side_effect = Exception("Docker failed")

        execute_test_run_test_case(self.pivot.pk)

        self.pivot.refresh_from_db()
        self.assertEqual(self.pivot.status, TestRunTestCaseStatus.FAILED)
        self.assertIn("Docker failed", self.pivot.result)
        mock_close_client.assert_called_once_with(mock_client)


# ============================================================================
# TEST RUN SCREENSHOT MODEL TESTS
# ============================================================================


class TestRunScreenshotModelTests(TestCase):
    def setUp(self) -> None:
        self.project = Project.objects.create(name="Screenshot Proj")
        self.tc = TestCaseModel.objects.create(project=self.project, title="TC ss")
        self.tr = TestRun.objects.create(project=self.project)
        self.pivot = TestRunTestCase.objects.create(test_run=self.tr, test_case=self.tc)

    def test_should_create_screenshot(self) -> None:
        from django.core.files.base import ContentFile

        image_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
            "2mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
        ss = TestRunScreenshot.objects.create(
            test_run_test_case=self.pivot,
            image=ContentFile(image_bytes, name="test.png"),
            tool_name="vnc_take_screenshot",
        )
        self.assertIsNotNone(ss.pk)
        self.assertEqual(ss.tool_name, "vnc_take_screenshot")
        self.assertIsNotNone(ss.created_at)

    def test_should_return_str_representation(self) -> None:
        from django.core.files.base import ContentFile

        ss = TestRunScreenshot.objects.create(
            test_run_test_case=self.pivot,
            image=ContentFile(b"fake", name="test.png"),
            tool_name="browser_navigate",
        )
        self.assertIn("browser_navigate", str(ss))

    def test_should_cascade_delete_with_pivot(self) -> None:
        from django.core.files.base import ContentFile

        TestRunScreenshot.objects.create(
            test_run_test_case=self.pivot,
            image=ContentFile(b"fake", name="test.png"),
            tool_name="take_screenshot",
        )
        self.assertEqual(TestRunScreenshot.objects.count(), 1)
        self.pivot.delete()
        self.assertEqual(TestRunScreenshot.objects.count(), 0)

    def test_should_order_by_created_at(self) -> None:
        from django.core.files.base import ContentFile

        ss1 = TestRunScreenshot.objects.create(
            test_run_test_case=self.pivot,
            image=ContentFile(b"a", name="a.png"),
            tool_name="first",
        )
        ss2 = TestRunScreenshot.objects.create(
            test_run_test_case=self.pivot,
            image=ContentFile(b"b", name="b.png"),
            tool_name="second",
        )
        screenshots = list(self.pivot.screenshots.all())
        self.assertEqual(screenshots[0].pk, ss1.pk)
        self.assertEqual(screenshots[1].pk, ss2.pk)

    def test_upload_path_contains_pivot_id(self) -> None:
        from projects.models import _screenshot_upload_path

        ss = TestRunScreenshot(test_run_test_case=self.pivot)
        path = _screenshot_upload_path(ss, "1234_vnc.png")
        self.assertIn(f"trtc_{self.pivot.pk}", path)
        self.assertIn("1234_vnc.png", path)


# ============================================================================
# BUILD SCREENSHOT CALLBACK TESTS
# ============================================================================


class BuildScreenshotCallbackTests(TestCase):
    def setUp(self) -> None:
        self.project = Project.objects.create(name="SS Callback Proj")
        self.tc = TestCaseModel.objects.create(
            project=self.project, title="TC ss callback"
        )
        self.tr = TestRun.objects.create(project=self.project)
        self.pivot = TestRunTestCase.objects.create(test_run=self.tr, test_case=self.tc)

    def test_should_create_screenshot_record(self) -> None:
        callback = _build_screenshot_callback(self.pivot)
        # 1x1 transparent PNG
        b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
            "2mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
        callback(b64, "vnc_take_screenshot")
        self.assertEqual(TestRunScreenshot.objects.count(), 1)
        ss = TestRunScreenshot.objects.first()
        assert ss is not None
        self.assertEqual(ss.tool_name, "vnc_take_screenshot")
        self.assertEqual(ss.test_run_test_case, self.pivot)

    def test_should_create_multiple_screenshots(self) -> None:
        callback = _build_screenshot_callback(self.pivot)
        b64 = base64.b64encode(b"fake-image-data").decode()
        callback(b64, "tool_a")
        callback(b64, "tool_b")
        callback(b64, "tool_c")
        self.assertEqual(TestRunScreenshot.objects.count(), 3)
        tool_names = list(TestRunScreenshot.objects.values_list("tool_name", flat=True))
        self.assertIn("tool_a", tool_names)
        self.assertIn("tool_b", tool_names)
        self.assertIn("tool_c", tool_names)
