from __future__ import annotations

from django.urls import URLPattern, path

from projects import views

app_name = "projects"

urlpatterns: list[URLPattern] = [
    path("", views.project_list, name="list"),
    path("create/", views.project_create, name="create"),
    path("<int:project_id>/edit/", views.project_edit, name="edit"),
    path("<int:project_id>/archive/", views.project_archive, name="archive"),
    path("<int:project_id>/", views.project_detail, name="detail"),
    path(
        "<int:project_id>/regenerate-api-key/",
        views.project_regenerate_api_key,
        name="regenerate_api_key",
    ),
    path(
        "<int:project_id>/download-client/",
        views.download_controller_client,
        name="download_controller_client",
    ),
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
    path("<int:project_id>/test-runs/", views.test_run_list, name="test_run_list"),
    path(
        "<int:project_id>/test-runs/create/",
        views.test_run_create,
        name="test_run_create",
    ),
    path(
        "<int:project_id>/test-runs/<int:test_run_id>/",
        views.test_run_detail,
        name="test_run_detail",
    ),
    path(
        "<int:project_id>/test-runs/<int:test_run_id>/delete/",
        views.test_run_delete,
        name="test_run_delete",
    ),
    path(
        "<int:project_id>/test-runs/<int:test_run_id>/redo/",
        views.test_run_redo,
        name="test_run_redo",
    ),
    path(
        "<int:project_id>/test-runs/<int:test_run_id>/start/",
        views.test_run_start,
        name="test_run_start",
    ),
    path(
        "<int:project_id>/test-runs/<int:test_run_id>/abort/",
        views.test_run_abort,
        name="test_run_abort",
    ),
    path(
        "<int:project_id>/test-runs/<int:test_run_id>/add-cases/",
        views.test_run_add_cases,
        name="test_run_add_cases",
    ),
    path(
        "<int:project_id>/test-runs/<int:test_run_id>/remove-case/<int:pivot_id>/",
        views.test_run_remove_case,
        name="test_run_remove_case",
    ),
    path(
        "<int:project_id>/test-runs/<int:test_run_id>/cases/<int:pivot_id>/",
        views.test_run_case_detail,
        name="test_run_case_detail",
    ),
]
