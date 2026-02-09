from django.urls import URLPattern, path

from dashboard import views

app_name = "dashboard"

urlpatterns: list[URLPattern] = [
    path("", views.index, name="index"),
]
