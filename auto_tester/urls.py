from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import URLPattern, URLResolver, include, path

urlpatterns: list[URLResolver | URLPattern] = [
    path("accounts/", include("accounts.urls")),
    path("", include("dashboard.urls")),
    path("projects/", include("projects.urls")),
    path("admin/", admin.site.urls),
]

if settings.DEBUG:
    # Daphne (unlike runserver) does not serve static files by itself; nginx does in production.
    urlpatterns += staticfiles_urlpatterns()
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
