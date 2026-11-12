"""Forms of the admin editor of a finished run case (input only, no ORM).

Text fields are capped by the ``CASE_EDIT_*`` model constants: a text over a
cap is refused with a validation error, never truncated. Every form carries the
editor's ``base_key`` (``case_edit_checkpoint.edit_base_key``) so a stale
editor is refused by ``case_edit_write.lock_editable_case``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from django import forms
from django.core.files.uploadedfile import UploadedFile
from django.forms.formsets import BaseFormSet
from django.utils.datastructures import MultiValueDict

from projects.case_edit_checkpoint import (
    OUTCOME_FAIL,
    OUTCOME_PASS,
    StepEdit,
    StepRow,
)
from projects.models import (
    CASE_EDIT_DECISION_MAX_LENGTH,
    CASE_EDIT_STEP_DESCRIPTION_MAX_LENGTH,
    CASE_EDIT_STEP_ERROR_MAX_LENGTH,
    CASE_EDIT_STEP_EXPECTED_MAX_LENGTH,
    CASE_EDIT_STEP_RESULT_MAX_LENGTH,
    CASE_EDIT_SUMMARY_MAX_LENGTH,
    EvidenceOverride,
    TestRunTestCaseStatus,
)

BASE_KEY_MAX_LENGTH = 64

_STATUS_CHOICES: list[tuple[str, str]] = [
    (TestRunTestCaseStatus.SUCCESS, "Passed"),
    (TestRunTestCaseStatus.FAILED, "Failed"),
    (TestRunTestCaseStatus.BLOCKED, "Blocked"),
]

_OUTCOME_CHOICES: list[tuple[str, str]] = [
    ("", "Did not run"),
    (OUTCOME_PASS, "Passed"),
    (OUTCOME_FAIL, "Failed"),
]


def _base_key_field() -> forms.CharField:
    return forms.CharField(max_length=BASE_KEY_MAX_LENGTH, widget=forms.HiddenInput)


class NewlineCharField(forms.CharField):
    """A ``CharField`` that turns ``\r\n`` and ``\r`` into ``\n``.

    Browsers submit textarea line breaks as CRLF; the stored texts (and the
    ``max_length`` cap) use plain ``\n`` like every other writer.
    """

    def to_python(self, value: object) -> str | None:
        text = super().to_python(value)
        if text is None:
            return None
        return text.replace("\r\n", "\n").replace("\r", "\n")


def _text_field(max_length: int, *, required: bool = False) -> NewlineCharField:
    # strip=False: what the admin typed is stored as typed; the cap refuses.
    return NewlineCharField(
        max_length=max_length,
        required=required,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )


class CaseResultEditForm(forms.Form):
    """The case's final status and result summary."""

    status = forms.ChoiceField(choices=_STATUS_CHOICES)
    result = _text_field(CASE_EDIT_SUMMARY_MAX_LENGTH)
    base_key = _base_key_field()


class CaseStepEditForm(forms.Form):
    """One plan step: wording, expected result, outcome and result texts."""

    step_num = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    description = _text_field(CASE_EDIT_STEP_DESCRIPTION_MAX_LENGTH, required=True)
    expected_result = _text_field(CASE_EDIT_STEP_EXPECTED_MAX_LENGTH)
    outcome = forms.ChoiceField(choices=_OUTCOME_CHOICES, required=False)
    summary = _text_field(CASE_EDIT_STEP_RESULT_MAX_LENGTH)
    error = _text_field(CASE_EDIT_STEP_ERROR_MAX_LENGTH)
    decision_reason = _text_field(CASE_EDIT_DECISION_MAX_LENGTH)


# Fixed count (the plan length, from ``initial``): no extra, no add/delete.
CaseStepEditFormSet = forms.formset_factory(CaseStepEditForm, extra=0)


def step_formset_initial(rows: Sequence[StepRow]) -> list[dict[str, object]]:
    """The formset's initial values, one entry per plan step in order."""
    return [
        {
            "step_num": row.step_num,
            "description": row.description,
            "expected_result": row.expected_result,
            "outcome": row.outcome,
            "summary": row.summary,
            "error": row.error,
            "decision_reason": row.decision_reason,
        }
        for row in rows
    ]


def cleaned_step_edits(formset: BaseFormSet[CaseStepEditForm]) -> list[StepEdit]:
    """The valid formset's steps as ``StepEdit`` values (outcome "" -> None)."""
    edits: list[StepEdit] = []
    for form in formset.forms:
        data = form.cleaned_data
        outcome = str(data.get("outcome") or "")
        edits.append(
            StepEdit(
                step_num=int(data["step_num"]),
                description=str(data["description"]),
                expected_result=str(data.get("expected_result") or ""),
                outcome=outcome or None,
                summary=str(data.get("summary") or ""),
                error=str(data.get("error") or ""),
                decision_reason=str(data.get("decision_reason") or ""),
            )
        )
    return edits


class ScreenshotEvidenceForm(forms.Form):
    """Whether one screenshot is attached to TestRail sends."""

    override = forms.ChoiceField(choices=EvidenceOverride.choices, required=False)
    base_key = _base_key_field()


class ScreenshotDeleteForm(forms.Form):
    base_key = _base_key_field()


class ScreenshotUploadForm(forms.Form):
    """A new screenshot, optionally tied to a plan step."""

    image = forms.FileField()
    step_num = forms.TypedChoiceField(
        coerce=int, empty_value=None, required=False, choices=[]
    )
    base_key = _base_key_field()

    def __init__(
        self,
        step_nums: Sequence[int],
        data: Mapping[str, object] | None = None,
        files: MultiValueDict[str, UploadedFile] | None = None,
        *,
        initial: dict[str, object] | None = None,
    ) -> None:
        super().__init__(data, files, initial=initial)
        field = self.fields["step_num"]
        assert isinstance(field, forms.TypedChoiceField)
        field.choices = [("", "No step")] + [
            (str(num), f"Step {num}") for num in step_nums
        ]


class CaseEditRevertForm(forms.Form):
    confirm = forms.BooleanField(required=True, widget=forms.HiddenInput)
