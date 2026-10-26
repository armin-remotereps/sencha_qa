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
    path("result/<int:pivot_id>/", views.result_chat_open, name="result_chat_open"),
    path(
        "result-changes/<int:change_id>/apply/",
        views.result_change_apply,
        name="result_change_apply",
    ),
    path(
        "result-changes/<int:change_id>/discard/",
        views.result_change_discard,
        name="result_change_discard",
    ),
    path(
        "redesigns/<int:revision_id>/rerun/",
        views.plan_redesign_rerun,
        name="plan_redesign_rerun",
    ),
    path(
        "redesigns/<int:revision_id>/discard/",
        views.plan_redesign_discard,
        name="plan_redesign_discard",
    ),
    path("documents/<int:document_id>/", views.setup_document, name="document"),
    path(
        "run-proposals/<int:proposal_id>/create/",
        views.run_proposal_create,
        name="run_proposal_create",
    ),
    path(
        "run-proposals/<int:proposal_id>/discard/",
        views.run_proposal_discard,
        name="run_proposal_discard",
    ),
]
