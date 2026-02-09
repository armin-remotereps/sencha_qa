from django.conf import settings
from django.contrib.auth import logout
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import redirect, render
from django.views import View

from accounts.forms import LoginForm
from accounts.services import authenticate_user, login_user


class LoginView(View):
    def get(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect(settings.LOGIN_REDIRECT_URL)
        form = LoginForm()
        return render(request, "accounts/login.html", {"form": form})

    def post(self, request: HttpRequest) -> HttpResponse:
        form = LoginForm(request.POST)
        if not form.is_valid():
            return render(request, "accounts/login.html", {"form": form})

        email = form.cleaned_data["email"]
        password = form.cleaned_data["password"]
        user = authenticate_user(request, email, password)

        if user is None:
            return render(
                request,
                "accounts/login.html",
                {"form": form, "error": "Invalid email or password"},
            )

        login_user(request, user)
        next_url = self._get_redirect_url(request)
        return redirect(next_url)

    def _get_redirect_url(self, request: HttpRequest) -> str:
        return (
            request.POST.get("next")
            or request.GET.get("next")
            or settings.LOGIN_REDIRECT_URL
        )


class LogoutView(View):
    def post(self, request: HttpRequest) -> HttpResponse:
        logout(request)
        return redirect("accounts:login")

    def get(self, request: HttpRequest) -> HttpResponseNotAllowed:
        return HttpResponseNotAllowed(["POST"])
