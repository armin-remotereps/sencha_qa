from django.test import TestCase
from django.urls import reverse

from accounts.models import CustomUser


class DashboardAccessTests(TestCase):
    def setUp(self) -> None:
        self.user = CustomUser.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpassword123",
            first_name="John",
            last_name="Doe",
        )

    def test_unauthenticated_redirects_to_login(self) -> None:
        response = self.client.get(reverse("dashboard:index"))
        self.assertRedirects(
            response,
            f"{reverse('accounts:login')}?next={reverse('dashboard:index')}",
        )

    def test_authenticated_returns_200(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get(reverse("dashboard:index"))
        self.assertEqual(response.status_code, 200)

    def test_response_contains_user_info(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get(reverse("dashboard:index"))
        self.assertContains(response, "John")
        self.assertContains(response, "Doe")
        self.assertContains(response, "test@example.com")

    def test_response_contains_logout_form(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get(reverse("dashboard:index"))
        self.assertContains(response, reverse("accounts:logout"))
        self.assertContains(response, 'method="post"')
