from django.urls import URLPattern, path

from test_sessions import views

app_name = "test_sessions"

urlpatterns: list[URLPattern] = [
    path("test-sessions/", views.session_list, name="list"),
    path("test-sessions/start/", views.session_start, name="start"),
    path("test-sessions/<int:session_id>/", views.session_detail, name="detail"),
    path("test-sessions/<int:session_id>/stop/", views.session_stop, name="stop"),
    path("test-sessions/<int:session_id>/edit/", views.session_edit, name="edit"),
    path(
        "test-sessions/<int:session_id>/delete/",
        views.session_delete,
        name="delete",
    ),
    path(
        "test-sessions/<int:session_id>/export.csv",
        views.session_export,
        name="export",
    ),
]
