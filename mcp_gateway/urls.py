from __future__ import annotations

from django.urls import URLPattern, path

from mcp_gateway import views

app_name = "mcp_gateway"

urlpatterns: list[URLPattern] = [
    path("", views.connections, name="connections"),
    path("authorize/<str:handle>/", views.consent, name="consent"),
    path("<int:grant_id>/revoke/", views.revoke, name="revoke"),
]
