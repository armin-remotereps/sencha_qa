"""The first message of a run's case analysis thread (fenced data)."""

from __future__ import annotations

from django.db.models import Count

from projects.models import TestRun, TestRunTestCase, TestRunTestCaseStatus
from run_triage.services.agent_texts import triage_overview_text
from run_triage.services.taxonomy_text import triage_taxonomy
from run_triage.tool_text import fenced, one_line

_INTRO = (
    "A person opened this case analysis thread for one test run. You analyse "
    "finished results; you have no access to the test machine. Classify the "
    "run's cases with classify_run_cases until none remain, group them with "
    "group_triage_cases, review with get_triage_overview and "
    "list_triage_cases, and look closer at doubtful cases with "
    "get_triage_case, read_case_log and ask_case_screenshot. Change the "
    "analysis (update_triage_cases, record_triage_groups) only when the "
    "person asks for it. Refer to cases as T<TestRail test id> or R<run case "
    "id>, as the tools do."
)


def build_triage_brief(test_run: TestRun) -> str:
    """Run name, mode, status counts, the taxonomy and the current analysis
    (about five queries)."""
    counts = dict(
        TestRunTestCase.objects.filter(test_run_id=test_run.id)
        .values_list("status")
        .annotate(total=Count("id"))
        .values_list("status", "total")
    )
    status_line = ", ".join(
        f"{label}: {counts[value]}"
        for value, label in TestRunTestCaseStatus.choices
        if counts.get(value)
    )
    taxonomy = triage_taxonomy()
    categories = "\n".join(
        f"- {label} [{value}]: {meaning}"
        for value, label, meaning in taxonomy.categories
    )
    severities = "\n".join(
        f"- {label} [{value}]: {meaning}"
        for value, label, meaning in taxonomy.severities
    )
    run_text = (
        f"Run: {one_line(test_run.display_name)} "
        f"({test_run.get_status_display()}, mode {test_run.get_help_mode_display()})\n"
        f"Cases by status: {status_line or 'none'}"
    )
    return "\n\n".join(
        [
            _INTRO,
            "TEST RUN\n" + fenced(run_text),
            "CATEGORIES (pick exactly one per case)\n" + categories,
            "SEVERITIES (impact on the release)\n" + severities,
            "CURRENT ANALYSIS\n" + triage_overview_text(test_run.id),
        ]
    )
