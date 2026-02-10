from __future__ import annotations

from django.urls import URLPattern, path

from projects import views

app_name = "projects"

urlpatterns: list[URLPattern] = [
    path("", views.project_list, name="list"),
    path("create/", views.project_create, name="create"),
    path("<int:project_id>/edit/", views.project_edit, name="edit"),
    path("<int:project_id>/archive/", views.project_archive, name="archive"),
    path(
        "<int:project_id>/test-cases/",
        views.test_case_list,
        name="test_case_list",
    ),
    path(
        "<int:project_id>/test-cases/create/",
        views.test_case_create,
        name="test_case_create",
    ),
    path(
        "<int:project_id>/test-cases/<int:test_case_id>/edit/",
        views.test_case_edit,
        name="test_case_edit",
    ),
    path(
        "<int:project_id>/test-cases/<int:test_case_id>/delete/",
        views.test_case_delete,
        name="test_case_delete",
    ),
    path(
        "<int:project_id>/uploads/",
        views.upload_list,
        name="upload_list",
    ),
    path(
        "<int:project_id>/uploads/create/",
        views.upload_create,
        name="upload_create",
    ),
    path(
        "<int:project_id>/uploads/<int:upload_id>/cancel/",
        views.upload_cancel,
        name="upload_cancel",
    ),
    path(
        "<int:project_id>/uploads/<int:upload_id>/delete/",
        views.upload_delete,
        name="upload_delete",
    ),
]
