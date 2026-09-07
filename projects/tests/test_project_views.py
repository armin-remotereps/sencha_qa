from __future__ import annotations

from django.test import Client, TestCase
from django.urls import reverse

from projects.tests.helpers import make_case, make_project, make_user


class ProjectRestoreTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)
        self.project.archived = True
        self.project.save()

    def test_restore_unarchives_project(self) -> None:
        client = Client()
        client.force_login(self.user)
        response = client.post(reverse("projects:restore", args=[self.project.id]))
        self.assertRedirects(response, reverse("projects:list"))
        self.project.refresh_from_db()
        self.assertFalse(self.project.archived)

    def test_restore_preserves_archived_query_param(self) -> None:
        client = Client()
        client.force_login(self.user)
        response = client.post(
            reverse("projects:restore", args=[self.project.id]), {"archived": "1"}
        )
        self.assertRedirects(
            response,
            f"{reverse('projects:list')}?archived=1",
            fetch_redirect_response=False,
        )

    def test_non_member_gets_404(self) -> None:
        other_user = make_user(email="other@example.com")
        client = Client()
        client.force_login(other_user)
        response = client.post(reverse("projects:restore", args=[self.project.id]))
        self.assertEqual(response.status_code, 404)


class ProjectListArchivedFilterTests(TestCase):
    def test_show_archived_context_reflects_query_param(self) -> None:
        user = make_user()
        client = Client()
        client.force_login(user)

        response = client.get(reverse("projects:list"))
        self.assertFalse(response.context["show_archived"])

        response = client.get(reverse("projects:list"), {"archived": "1"})
        self.assertTrue(response.context["show_archived"])

    def test_archived_projects_hidden_by_default(self) -> None:
        user = make_user()
        active = make_project(user=user, name="Active")
        archived = make_project(user=user, name="Archived")
        archived.archived = True
        archived.save()
        client = Client()
        client.force_login(user)

        response = client.get(reverse("projects:list"))

        names = {project.name for project in response.context["projects"]}
        self.assertIn(active.name, names)
        self.assertNotIn(archived.name, names)


class FormerlySilentInvalidFormTests(TestCase):
    def setUp(self) -> None:
        self.user = make_user()
        self.project = make_project(user=self.user)

    def test_project_edit_invalid_form_shows_message(self) -> None:
        client = Client()
        client.force_login(self.user)
        response = client.post(
            reverse("projects:edit", args=[self.project.id]),
            {"name": ""},
            follow=True,
        )
        messages = list(response.context["messages"])
        self.assertTrue(any("Name" in str(m) for m in messages))

    def test_test_case_create_invalid_form_shows_message(self) -> None:
        client = Client()
        client.force_login(self.user)
        response = client.post(
            reverse("projects:test_case_create", args=[self.project.id]),
            {"title": ""},
            follow=True,
        )
        messages = list(response.context["messages"])
        self.assertGreaterEqual(len(messages), 1)

    def test_test_case_copy_to_project_without_selection_shows_message(self) -> None:
        target = make_project(user=self.user, name="Target")
        client = Client()
        client.force_login(self.user)
        response = client.post(
            reverse("projects:test_case_copy_to_project", args=[self.project.id]),
            {"target_project_id": str(target.id)},
            follow=True,
        )
        messages = list(response.context["messages"])
        self.assertTrue(
            any("Select at least one test case" in str(m) for m in messages)
        )

    def test_test_case_bulk_delete_removes_selected_cases(self) -> None:
        case = make_case(project=self.project)
        client = Client()
        client.force_login(self.user)
        response = client.post(
            reverse("projects:test_case_bulk_delete", args=[self.project.id]),
            {"test_case_ids": [str(case.id)]},
            follow=True,
        )
        messages = list(response.context["messages"])
        self.assertTrue(any("Deleted 1 test cases" in str(m) for m in messages))
        self.assertFalse(self.project.test_cases.filter(id=case.id).exists())
