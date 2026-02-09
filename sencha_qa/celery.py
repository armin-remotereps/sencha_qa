import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sencha_qa.settings")

app = Celery("sencha_qa")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
