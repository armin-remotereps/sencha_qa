from __future__ import annotations

from django.urls import URLPattern, path

from machine_readiness import views

app_name = "machine_readiness"

# Mounted at projects/<int:project_id>/machine-readiness/ in auto_tester/urls.py.
urlpatterns: list[URLPattern] = [
    path("", views.overview, name="overview"),
    path("artifacts/edit/", views.artifacts_edit, name="artifacts_edit"),
    path(
        "artifacts/versions/<int:version_id>/restore/",
        views.artifacts_version_restore,
        name="artifacts_version_restore",
    ),
    path("intake/skip/", views.intake_skip, name="intake_skip"),
    # scan/ is projects:machine_readiness_scan (it needs the runner ports).
    path("requirements/", views.requirements, name="requirements"),
    path(
        "requirements/analyse/",
        views.requirements_analyse,
        name="requirements_analyse",
    ),
    path(
        "requirements/<int:requirement_id>/",
        views.requirement_update,
        name="requirement_update",
    ),
]
