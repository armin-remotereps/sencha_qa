from django.urls import URLPattern, path

from dashboard import failure_views, health_views, views

app_name = "dashboard"

urlpatterns: list[URLPattern] = [
    path("", views.index, name="index"),
    path("attention/", views.attention_fragment, name="attention_fragment"),
    path("failures/", failure_views.failed_cases, name="failed_cases"),
    path(
        "failures/export.csv",
        failure_views.failed_cases_export,
        name="failed_cases_export",
    ),
    path("failures/bulk/", failure_views.failed_cases_bulk, name="failed_cases_bulk"),
    path("failures/presets/", failure_views.preset_save, name="preset_save"),
    path(
        "failures/presets/<int:preset_id>/delete/",
        failure_views.preset_delete,
        name="preset_delete",
    ),
    path("insights/machines/", health_views.machine_health, name="machine_health"),
]
