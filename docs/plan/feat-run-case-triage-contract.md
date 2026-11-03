# Run case triage: implementation contract (shared by the sub-agents)

Plan: `docs/plan/feat-run-case-triage.md`. Already written (do not redefine):
`run_triage/{apps,models,taxonomy,case_scope,exceptions,types,admin}.py`,
migrations `run_triage/0001_initial`, `machine_setup/0012_run_triage_thread`
(`SetupConversationKind.RUN_TRIAGE`, `SetupConversation.triage_run`),
`ThreadRole.RUN_TRIAGE` + `thread_role(triage_run_id=)`,
`agents/triage_types.py`, the `RUN_TRIAGE_*` settings (see
`auto_tester/settings.py`), `"run_triage"` in `INSTALLED_APPS`.

Every module: full type annotations (mypy strict), imports at top, black/isort,
match surrounding style and comment density. No tests. Never edit a file owned
by another part.

## Part A — AI calls (`agents/services/`)

Files: `run_case_classifier.py`, `run_case_classifier_prompts.py`,
`run_case_grouper.py`, `run_case_grouper_prompts.py`. No ORM, no `run_triage`
import. Pattern: `agents/services/manual_result_refiner.py` (tool-free,
strict JSON, code validation, `AIServiceError` on unusable answers).

```python
def classify_run_cases(
    llm: LLMConfig,
    cases: tuple[TriageCaseInput, ...],
    *,
    application_context: str,
    taxonomy: TriageTaxonomyText,
    timeout_seconds: float,
) -> tuple[CaseClassification, ...]
```
One call; each case fenced as data under its `ref`; screenshots attached as
`ImageContent` parts each preceded by a `TextContent` label "Screenshot of
<ref>, step N". Returns exactly one classification per input ref (input order).
Values must be in the taxonomy; finding non-empty, single line, ≤ 300 chars
(cut). Raises `AIServiceError` when the answer is unusable (bad JSON, a ref
missing/duplicated/unknown, bad enum) so the caller can retry in halves.

```python
def group_category_cases(
    llm: LLMConfig,
    category: tuple[str, str, str],   # (value, label, meaning)
    items: tuple[GroupItem, ...],
    *,
    application_context: str,
    timeout_seconds: float,
) -> CategoryGrouping
```
One call. Groups cases sharing one root cause under a one-line finding
(≤ 300), optional one sub-group level (e.g. per chart type), a category note
(≤ 2000). Code validation: drop unknown refs, keep the first occurrence of a
duplicated ref, drop empty groups; refs the answer omitted are NOT added here
(the caller puts them in "Other findings"). `AIServiceError` on bad JSON.

## Part B — `run_triage` services

Files: `run_triage/case_refs.py`, `run_triage/markdown_export.py`,
`run_triage/services/{turn_gate,triage_input,classify_cases,group_cases,correct_cases,triage_brief,read_triage,triage_export,export_throttle}.py`.
Import rule (plan › Apps and import rules): from `projects` only `models`,
`exceptions`, `testrail_results_services`, `testrail_evidence`,
`manual_run_policy`, `run_machine_use`; from `machine_setup` only `models` and
`turn_state`; never `machine_setup.services`, `projects.services`.

Public interface (other parts call exactly these):

```python
# run_triage/case_refs.py
def case_ref(pivot_id: int, testrail_test_id: int | None) -> str   # "T<id>" or "R<pivot_id>"
def resolve_case_ref(test_run_id: int, ref: str) -> int            # pivot id; RunTriageError if not a case of this run

# run_triage/services/turn_gate.py
def require_running_turn(turn_id: int) -> None
    # call inside transaction.atomic(): select_for_update the SetupTurn, raise
    # TriageTurnClosedError unless status == RUNNING.

# run_triage/services/classify_cases.py
ClassifyScope = Literal["pending", "all", "cases"]
def classify_run_cases(*, test_run_id: int, turn_id: int, scope: ClassifyScope,
                       refs: tuple[str, ...], should_stop: Callable[[], bool]) -> str
    # tool result text: "Classified 48 (12 reused, 0 failed); 120 remain: call again."
    # + per-category counts. RunTriageError for bad refs / too many cases;
    # TriageTurnClosedError propagates.

# run_triage/services/group_cases.py
def group_triage_cases(*, test_run_id: int, turn_id: int,
                       categories: tuple[str, ...], should_stop: Callable[[], bool]) -> str   # outline text

# run_triage/services/correct_cases.py
@dataclass(frozen=True)
class CaseCorrection:
    ref: str
    category: str | None
    severity: str | None
    finding: str | None
    group: str | None
def correct_triage_cases(*, test_run_id: int, turn_id: int,
                         changes: tuple[CaseCorrection, ...], reason: str,
                         corrected_by_label: str) -> str
def record_triage_groups(*, test_run_id: int, turn_id: int, category: str,
                         note: str | None, groups: tuple[GroupResult, ...],
                         corrected_by_label: str) -> str
    # GroupResult / SubGroupResult from agents.triage_types (refs are case refs).

# run_triage/services/read_triage.py
def count_analysable(test_run_id: int) -> int
def describe_triage_status(test_run_id: int) -> str    # one line for the role status
def triage_overview_text(test_run_id: int) -> str
def list_triage_cases_text(test_run_id: int, category: str, page: int) -> str
def triage_case_text(test_run_id: int, ref: str) -> str   # case text, verdict, steps, triage, screenshot ids + step, log size
def get_triage_card(test_run: TestRun) -> TriageCard
def get_triage_report(test_run: TestRun, project: Project) -> TriageReport
def triage_badges_for(pivot_ids: Collection[int]) -> dict[int, TriageBadge]   # 1 query

# run_triage/services/triage_brief.py
def build_triage_brief(test_run: TestRun) -> str   # first message of a new thread; fenced data

# run_triage/markdown_export.py
def triage_markdown(report: TriageReport) -> str    # the user's example format

# run_triage/services/triage_export.py
def triage_csv_rows(test_run: TestRun, project: Project) -> Iterator[str]   # BOM+header+rows, neutralize_cell
def triage_csv_filename(test_run_id: int) -> str   # "run-<id>-case-analysis.csv"
# run_triage/services/export_throttle.py
def allow_triage_export(user_id: int) -> bool     # 5/min/user, cache, like test_sessions
```

## Part C — Machine setup integration

Files: everything under `machine_setup/` that the plan's step 4 lists
(`services/case_evidence_tools.py` new, `result_chat_tools.py`,
`services/triage_chat_tools.py` new, `setup_tools.py`, `run_turn.py`,
`setup_prompt.py`, `send_gates.py`, `send_message.py`,
`services/open_triage_chat.py` new, `services/analysis_threads.py` new,
`chat_page.py`, `exceptions.py`, `forms.py`, `views.py`, `urls.py`) and
`templates/machine_setup/**`. Consumes Part B's interface above only.

Exposes: URL name `machine_setup:triage_chat_open` (POST
`/projects/<project_id>/machine-setup/triage/`, form fields `test_run_id`,
`text` optional, `next` optional) and the thread page URL via the existing
`machine_setup:thread` name.

## Part D — Report page, CSV, run page and case page

Files: `run_triage/views.py`, `run_triage/urls.py`, `auto_tester/urls.py`
(or the project URL include where project-scoped app URLs live),
`templates/run_triage/**`, `static/css/punk-hazard.css`, `projects/views.py`,
`templates/projects/test_run_detail.html`,
`templates/projects/test_run_case_detail.html` (and the run case table
partial they include, if that is where rows render). Consumes Part B's read
interface and DTOs in `run_triage/types.py`; posts Analyze to
`machine_setup:triage_chat_open` with `test_run_id` and `next`; links Open
chat to `machine_setup:thread` with `conversation_id=card.thread_id`.

URL names: `run_triage:report` (`/projects/<project_id>/test-runs/<run_id>/triage/`),
`run_triage:export_csv` (`.../triage/export.csv`).
