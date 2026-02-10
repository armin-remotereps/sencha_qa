import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "auto_tester.settings")

app = Celery("auto_tester")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
