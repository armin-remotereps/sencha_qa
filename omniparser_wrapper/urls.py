from django.urls import URLPattern, path

from omniparser_wrapper import views

urlpatterns: list[URLPattern] = [
    path("health/", views.health),
    path("ready/", views.ready),
    path("parse/", views.parse_screenshot),
    path("parse/pixels/", views.parse_screenshot_pixels),
]
