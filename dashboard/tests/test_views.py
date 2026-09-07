from __future__ import annotations

from django.test import Client, TestCase
from django.urls import reverse

from projects.tests.helpers import make_project, make_user


class DashboardIndexViewTests(TestCase):
    def test_anonymous_user_is_redirected_to_login(self) -> None:
        client = Client()
        response = client.get(reverse("dashboard:index"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)  # type: ignore[attr-defined]  # .url only exists on HttpResponseRedirectBase, which a 302 always is

    def test_authenticated_user_sees_dashboard_data(self) -> None:
        user = make_user()
        make_project(user=user)
        client = Client()
        client.force_login(user)

        response = client.get(reverse("dashboard:index"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["dashboard"].has_projects)
        self.assertEqual(response.context["active_nav"], "dashboard")

    def test_empty_state_when_user_has_no_projects(self) -> None:
        user = make_user()
        client = Client()
        client.force_login(user)

        response = client.get(reverse("dashboard:index"))

        self.assertFalse(response.context["dashboard"].has_projects)
