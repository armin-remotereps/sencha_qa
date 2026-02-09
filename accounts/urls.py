from django.urls import URLPattern, path

from accounts import views

app_name = "accounts"

urlpatterns: list[URLPattern] = [
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
]
