from __future__ import annotations

from django.db import models

from projects.models import TestRun, TestRunTestCase
from run_triage.taxonomy import (
    TRIAGE_FINDING_MAX_LENGTH,
    TRIAGE_GROUP_TITLE_MAX_LENGTH,
    TriageCategory,
    TriageSeverity,
    TriageSource,
)


class RunTriage(models.Model):
    """The case analysis of one run; created on its first write."""

    test_run = models.OneToOneField(
        TestRun, on_delete=models.CASCADE, related_name="triage"
    )
    # {"<category>": "note"}, written by grouping or a person's request.
    category_notes = models.JSONField(default=dict, blank=True)
    last_classified_at = models.DateTimeField(null=True, blank=True)
    last_grouped_at = models.DateTimeField(null=True, blank=True)
    prompt_version = models.PositiveSmallIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Case analysis of run {self.test_run_id}"


class TriageGroup(models.Model):
    """Cases of one category that share a root cause; one sub-group level."""

    triage = models.ForeignKey(
        RunTriage, on_delete=models.CASCADE, related_name="groups"
    )
    category = models.CharField(max_length=32, choices=TriageCategory.choices)
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="children",
    )
    # The group's finding (one line).
    title = models.CharField(max_length=TRIAGE_GROUP_TITLE_MAX_LENGTH)
    position = models.PositiveIntegerField(default=0)
    source = models.CharField(
        max_length=8, choices=TriageSource.choices, default=TriageSource.AI
    )

    class Meta:
        indexes = [
            models.Index(
                fields=["triage", "category", "position"],
                name="triage_group_order",
            )
        ]

    def __str__(self) -> str:
        return self.title


class TriageCase(models.Model):
    """One analysed run case: category, severity and finding."""

    triage = models.ForeignKey(
        RunTriage, on_delete=models.CASCADE, related_name="cases"
    )
    test_run_test_case = models.ForeignKey(
        TestRunTestCase, on_delete=models.CASCADE, related_name="triage_rows"
    )
    category = models.CharField(max_length=32, choices=TriageCategory.choices)
    severity = models.CharField(max_length=16, choices=TriageSeverity.choices)
    finding = models.CharField(max_length=TRIAGE_FINDING_MAX_LENGTH)
    # Cancelled with a result: counted as "Untested" in its category.
    untested = models.BooleanField(default=False)
    # Null until grouped.
    group = models.ForeignKey(
        TriageGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cases",
    )
    # sha256 of the classifier input: an unchanged case reuses its row.
    input_hash = models.CharField(max_length=64, blank=True, default="")
    # The case as it was when classified: a different status or finish time
    # now means the row is out of date.
    classified_status = models.CharField(max_length=20)
    classified_finished_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(
        max_length=8, choices=TriageSource.choices, default=TriageSource.AI
    )
    # Set by a person's correction through the analysis chat.
    corrected_by_label = models.CharField(max_length=200, blank=True, default="")
    corrected_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["triage", "test_run_test_case"],
                name="triage_one_row_per_case",
            )
        ]

    def __str__(self) -> str:
        return f"{self.category} / {self.severity} (case {self.test_run_test_case_id})"
