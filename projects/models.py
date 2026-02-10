from __future__ import annotations

from django.conf import settings
from django.db import models


class Tag(models.Model):
    name = models.CharField(max_length=50, unique=True, db_index=True)

    def __str__(self) -> str:
        return self.name


class Project(models.Model):
    name = models.CharField(max_length=255)
    tags = models.ManyToManyField(Tag, blank=True, related_name="projects")
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="projects"
    )
    archived = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.name
