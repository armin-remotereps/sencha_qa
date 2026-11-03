from __future__ import annotations

from django.urls import URLPattern, path

from run_triage import views

app_name = "run_triage"

# Mounted at projects/<int:project_id>/test-runs/<int:run_id>/triage/ in
# auto_tester/urls.py.
urlpatterns: list[URLPattern] = [
    path("", views.report, name="report"),
    path("export.csv", views.export_csv, name="export_csv"),
]
