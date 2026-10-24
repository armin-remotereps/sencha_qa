from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import URLPattern, URLResolver, include, path

urlpatterns: list[URLResolver | URLPattern] = [
    path("accounts/", include("accounts.urls")),
    path("", include("dashboard.urls")),
    # The Notifications page and its actions, all under /notifications/.
    path("", include("notifications.urls")),
    path("projects/", include("projects.urls")),
    path(
        "projects/<int:project_id>/machine-setup/",
        include("machine_setup.urls"),
    ),
    path("admin/", admin.site.urls),
]

if settings.DEBUG:
    # Daphne (unlike runserver) does not serve static files by itself; nginx does in production.
    urlpatterns += staticfiles_urlpatterns()
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
