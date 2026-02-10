from django.contrib.admin.sites import AdminSite
from django.db import IntegrityError
from django.test import RequestFactory, TestCase
from django.urls import reverse

from accounts.models import CustomUser
from projects.admin import ProjectAdmin, TagAdmin, TestCaseAdmin
from projects.forms import ProjectForm, TestCaseForm
from projects.models import Project, Tag
from projects.models import TestCase as TestCaseModel
from projects.models import TestCaseData, TestCasePriority, TestCaseType
from projects.services import (
    archive_project,
    create_project,
    create_test_case,
    delete_test_case,
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
