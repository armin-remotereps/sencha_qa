from django.urls import URLPattern, path

from accounts import user_views, views

app_name = "accounts"

urlpatterns: list[URLPattern] = [
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("users/", user_views.users, name="users"),
    path("users/create/", user_views.user_create, name="user_create"),
]
