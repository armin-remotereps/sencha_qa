"""The ``/runners/`` pages (mounted at ``runners/`` in ``auto_tester/urls.py``)."""

from __future__ import annotations

from django.urls import URLPattern, path

from runners import views

app_name = "runners"

urlpatterns: list[URLPattern] = [
    path("", views.runner_list, name="list"),
    path("add/", views.runner_add, name="add"),
    path("all/", views.runner_all, name="all"),
    path("<int:runner_id>/", views.runner_detail, name="detail"),
    path("<int:runner_id>/rename/", views.runner_rename, name="rename"),
    path("<int:runner_id>/download/", views.runner_download, name="download"),
    path("<int:runner_id>/token/", views.runner_token, name="token"),
    path("<int:runner_id>/update/", views.runner_update, name="update"),
    path("<int:runner_id>/release/", views.runner_release, name="release"),
    path("<int:runner_id>/disconnect/", views.runner_disconnect, name="disconnect"),
    path("<int:runner_id>/archive/", views.runner_archive, name="archive"),
]
