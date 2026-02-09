from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from accounts.models import CustomUser
from accounts.services import authenticate_user


class LoginPageTests(TestCase):
    def test_login_page_renders(self) -> None:
        response = self.client.get(reverse("accounts:login"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "accounts/login.html")

    def test_login_page_contains_form(self) -> None:
        response = self.client.get(reverse("accounts:login"))
        self.assertContains(response, "<form")
        self.assertContains(response, 'name="email"')
        self.assertContains(response, 'name="password"')


class LoginAuthTests(TestCase):
    def setUp(self) -> None:
        self.user = CustomUser.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="correctpassword123",
            first_name="John",
            last_name="Doe",
        )

    def test_valid_credentials_redirect_to_dashboard(self) -> None:
        response = self.client.post(
            reverse("accounts:login"),
            {"email": "test@example.com", "password": "correctpassword123"},
        )
        self.assertRedirects(response, reverse("dashboard:index"))

    def test_invalid_password_returns_error(self) -> None:
        response = self.client.post(
            reverse("accounts:login"),
            {"email": "test@example.com", "password": "wrongpassword"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid email or password")

    def test_nonexistent_email_returns_error(self) -> None:
        response = self.client.post(
            reverse("accounts:login"),
            {"email": "nobody@example.com", "password": "somepassword"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid email or password")

    def test_next_param_preserved_through_login(self) -> None:
        response = self.client.post(
            reverse("accounts:login") + "?next=/dashboard/",
            {"email": "test@example.com", "password": "correctpassword123"},
        )
        self.assertRedirects(response, "/dashboard/")

    def test_already_authenticated_redirects_to_dashboard(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get(reverse("accounts:login"))
        self.assertRedirects(response, reverse("dashboard:index"))


class LogoutTests(TestCase):
    def setUp(self) -> None:
        self.user = CustomUser.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpassword123",
        )

    def test_logout_requires_post(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get(reverse("accounts:logout"))
        self.assertEqual(response.status_code, 405)

    def test_logout_clears_session_and_redirects(self) -> None:
        self.client.force_login(self.user)
        response = self.client.post(reverse("accounts:logout"))
        self.assertRedirects(response, reverse("accounts:login"))
        dashboard_response = self.client.get(reverse("dashboard:index"))
        self.assertEqual(dashboard_response.status_code, 302)


class AuthenticateUserServiceTests(TestCase):
    def setUp(self) -> None:
        self.user = CustomUser.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="correctpassword123",
        )

    def test_authenticate_user_returns_user_for_valid_creds(self) -> None:
        factory = RequestFactory()
        request = factory.post("/accounts/login/")
        result = authenticate_user(request, "test@example.com", "correctpassword123")
        self.assertIsNotNone(result)
        self.assertEqual(result, self.user)

    def test_authenticate_user_returns_none_for_bad_creds(self) -> None:
        factory = RequestFactory()
        request = factory.post("/accounts/login/")
        result = authenticate_user(request, "test@example.com", "wrongpassword")
        self.assertIsNone(result)

    def test_authenticate_user_returns_none_for_nonexistent_email(self) -> None:
        factory = RequestFactory()
        request = factory.post("/accounts/login/")
        result = authenticate_user(request, "nobody@example.com", "password")
        self.assertIsNone(result)
