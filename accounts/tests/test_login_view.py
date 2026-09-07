from __future__ import annotations

from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import CustomUser


class LoginViewTests(TestCase):
    def setUp(self) -> None:
        self.user = CustomUser.objects.create_user(
            email="user@example.com", password="password123"
        )

    def test_invalid_credentials_rerenders_with_error_and_bound_form(self) -> None:
        client = Client()
        response = client.post(
            reverse("accounts:login"),
            {"email": "user@example.com", "password": "wrong"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["error"], "Invalid email or password")
        self.assertEqual(response.context["form"].data["email"], "user@example.com")

    def test_missing_fields_rerenders_with_field_errors(self) -> None:
        client = Client()
        response = client.post(reverse("accounts:login"), {"email": ""})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)

    def test_valid_login_redirects_to_safe_next_url(self) -> None:
        client = Client()
        response = client.post(
            f"{reverse('accounts:login')}?next=/projects/",
            {"email": "user@example.com", "password": "password123"},
        )
        self.assertRedirects(response, "/projects/", fetch_redirect_response=False)

    def test_unsafe_next_url_falls_back_to_login_redirect(self) -> None:
        client = Client()
        response = client.post(
            f"{reverse('accounts:login')}?next=https://evil.example.com/",
            {"email": "user@example.com", "password": "password123"},
        )
        self.assertNotEqual(response.url, "https://evil.example.com/")  # type: ignore[attr-defined]  # .url only exists on HttpResponseRedirectBase, which a 302 always is
