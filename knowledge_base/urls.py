from __future__ import annotations

from django.urls import URLPattern, path

from knowledge_base import views

app_name = "knowledge_base"

# Mounted at projects/<int:project_id>/knowledge-base/ in auto_tester/urls.py.
urlpatterns: list[URLPattern] = [
    path("", views.knowledge_list, name="list"),
    path("upload/", views.knowledge_upload, name="upload"),
    path("items/<int:item_id>/", views.knowledge_item, name="item"),
    path("items/<int:item_id>/edit/", views.knowledge_item_edit, name="item_edit"),
    path("items/<int:item_id>/retry/", views.knowledge_item_retry, name="item_retry"),
    path(
        "items/<int:item_id>/delete/", views.knowledge_item_delete, name="item_delete"
    ),
    path(
        "items/<int:item_id>/download/",
        views.knowledge_item_download,
        name="item_download",
    ),
]
