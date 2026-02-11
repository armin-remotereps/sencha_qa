from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import URLPattern, URLResolver, include, path

urlpatterns: list[URLResolver | URLPattern] = [
    path("accounts/", include("accounts.urls")),
    path("", include("dashboard.urls")),
    path("projects/", include("projects.urls")),
    path("admin/", admin.site.urls),
    path("omniparser/", include("omniparser.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
