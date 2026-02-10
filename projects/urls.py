from __future__ import annotations

from django.urls import URLPattern, path

from projects import views

app_name = "projects"

urlpatterns: list[URLPattern] = [
    path("", views.project_list, name="list"),
    path("create/", views.project_create, name="create"),
    path("<int:project_id>/edit/", views.project_edit, name="edit"),
    path("<int:project_id>/archive/", views.project_archive, name="archive"),
]
