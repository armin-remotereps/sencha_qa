from __future__ import annotations

from django.urls import URLPattern, path

from machine_setup import views

app_name = "machine_setup"

# Mounted at projects/<int:project_id>/machine-setup/ in auto_tester/urls.py.
urlpatterns: list[URLPattern] = [
    path("", views.setup_chat, name="chat"),
    path("threads/<int:conversation_id>/", views.setup_thread, name="thread"),
    path("messages/", views.setup_send, name="send"),
    path("stop/", views.setup_stop, name="stop"),
    path("reset/", views.setup_reset, name="reset"),
    path("help/<int:pivot_id>/", views.help_chat_open, name="help_chat_open"),
]
