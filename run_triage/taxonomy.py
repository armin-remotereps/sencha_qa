"""The fixed case analysis taxonomy: categories and severities (leaf, no ORM)."""

from __future__ import annotations

from django.db import models

# Part of every input hash: bump it when the prompt or taxonomy changes so
# "Analyze again" re-classifies instead of reusing rows.
TRIAGE_PROMPT_VERSION = 1


class TriageCategory(models.TextChoices):
    """Display order is declaration order."""

    PRODUCT_FUNCTIONAL = "product_functional", "Product bug: functional"
    PRODUCT_CONSOLE = "product_console", "Product bug: console errors only"
    EXPECTATION_NOT_MET = "expectation_not_met", "Expectation not met"
    TEST_CASE_ISSUE = "test_case_issue", "Test case issue"
    TEST_SCENARIO_ISSUE = "test_scenario_issue", "Test scenario / harness issue"
    ENVIRONMENT = "environment", "Environment / infrastructure"
    EXECUTION_ERROR = "execution_error", "Test execution error"
    OTHER = "other", "Other"


# One line per category, given to the AI so it picks consistently.
CATEGORY_MEANINGS: dict[str, str] = {
    TriageCategory.PRODUCT_FUNCTIONAL: (
        "The application under test misbehaves: crashes, wrong results, broken "
        "features, missing packages it should ship, security defects."
    ),
    TriageCategory.PRODUCT_CONSOLE: (
        "The behaviour is right but the application logs errors (console, build "
        "output) that point to a product defect."
    ),
    TriageCategory.EXPECTATION_NOT_MET: (
        "The feature works, but differs from what the case expects (layout, a "
        "missing button, an extra column) in a way that may be intended."
    ),
    TriageCategory.TEST_CASE_ISSUE: (
        "The test case itself is wrong: outdated paths or names, impossible or "
        "contradictory expectations, scheduled on the wrong platform."
    ),
    TriageCategory.TEST_SCENARIO_ISSUE: (
        "The test scenario or harness reports wrongly (a status banner or "
        "indicator does not update) although the behaviour is correct."
    ),
    TriageCategory.ENVIRONMENT: (
        "The test machine, Test Runner, network, credentials, installed tools or "
        "an AI service prevented a valid result."
    ),
    TriageCategory.EXECUTION_ERROR: (
        "The agent or the tester performed or judged a step wrongly; the product "
        "was not shown to be at fault."
    ),
    TriageCategory.OTHER: "None of the above fits.",
}


class TriageSeverity(models.TextChoices):
    """Impact on the release, for every category (most severe first)."""

    CRITICAL = "critical", "Critical"
    MAJOR = "major", "Major"
    MINOR = "minor", "Minor"
    TRIVIAL = "trivial", "Trivial"


SEVERITY_MEANINGS: dict[str, str] = {
    TriageSeverity.CRITICAL: (
        "Blocks a release: the product does not start or install, data loss, "
        "security, a core flow unusable for everyone."
    ),
    TriageSeverity.MAJOR: "A main feature is broken or many cases are affected.",
    TriageSeverity.MINOR: "A limited or cosmetic defect, or a workaround exists.",
    TriageSeverity.TRIVIAL: "No user impact (wording, noise, a test-only issue).",
}


class TriageSource(models.TextChoices):
    AI = "ai", "AI"
    PERSON = "person", "Person"


# Caps on stored AI and person text.
TRIAGE_FINDING_MAX_LENGTH = 300
TRIAGE_GROUP_TITLE_MAX_LENGTH = 300
TRIAGE_NOTE_MAX_LENGTH = 2000
