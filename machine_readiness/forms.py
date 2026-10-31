"""Forms of the Machine readiness tab (artifacts profile, requirements)."""

from __future__ import annotations

from typing import cast

from django import forms
from django.forms import BaseFormSet, formset_factory

from machine_readiness.keys import key_error
from machine_readiness.models import (
    BASE_DIR_MAX_LENGTH,
    INSTALL_HINT_MAX_LENGTH,
    ITEM_EXPECTED_VERSION_MAX_LENGTH,
    ITEM_NAME_MAX_LENGTH,
    ITEM_PATH_PATTERN_MAX_LENGTH,
    ITEM_SOURCE_MAX_LENGTH,
    KEY_MAX_LENGTH,
    PROFILE_CHANGE_SUMMARY_MAX_LENGTH,
    PROFILE_MAX_ITEMS,
    PROFILE_NOTES_MAX_LENGTH,
    REQUIREMENT_CHECK_TARGET_MAX_LENGTH,
    REQUIREMENT_STATUS_DETAIL_MAX_LENGTH,
    REQUIREMENT_TITLE_MAX_LENGTH,
    ArtifactKind,
    ArtifactOs,
    RequirementKind,
    RequirementStatus,
)
from machine_readiness.profile_rules import glob_error, validate_base_dir
from machine_readiness.types import ProfileData, ProfileItemData

# Empty rows offered below the saved artifacts on the edit page.
ITEM_FORMSET_EXTRA = 3


def _base_dir_field(placeholder: str) -> forms.CharField:
    return forms.CharField(
        required=False,
        max_length=BASE_DIR_MAX_LENGTH,
        widget=forms.TextInput(
            attrs={"class": "input", "placeholder": placeholder, "spellcheck": "false"}
        ),
    )


class ArtifactProfileForm(forms.Form):
    """Base directories, notes and the change summary; ``base_revision`` hidden."""

    base_dir_windows = _base_dir_field(r"D:\qa or \\server\share\qa")
    base_dir_macos = _base_dir_field("~/qa or /opt/qa")
    base_dir_linux = _base_dir_field("~/qa or /opt/qa")
    notes = forms.CharField(
        required=False,
        max_length=PROFILE_NOTES_MAX_LENGTH,
        widget=forms.Textarea(attrs={"class": "textarea", "rows": 6}),
    )
    change_summary = forms.CharField(
        required=False,
        max_length=PROFILE_CHANGE_SUMMARY_MAX_LENGTH,
        widget=forms.TextInput(
            attrs={"class": "input", "placeholder": "What changed (optional)"}
        ),
    )
    base_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    def clean_base_dir_windows(self) -> str:
        return self._base_dir(ArtifactOs.WINDOWS, "base_dir_windows")

    def clean_base_dir_macos(self) -> str:
        return self._base_dir(ArtifactOs.MACOS, "base_dir_macos")

    def clean_base_dir_linux(self) -> str:
        return self._base_dir(ArtifactOs.LINUX, "base_dir_linux")

    def _base_dir(self, os_name: ArtifactOs, field: str) -> str:
        value: str = self.cleaned_data[field]
        problem = validate_base_dir(os_name, value)
        if problem:
            raise forms.ValidationError(problem, code="base_dir")
        return value


class ArtifactItemForm(forms.Form):
    """One artifact row; a blank extra row is ignored, DELETE drops a row."""

    key = forms.CharField(
        max_length=KEY_MAX_LENGTH,
        widget=forms.TextInput(
            attrs={"class": "input", "placeholder": "jdk-17", "spellcheck": "false"}
        ),
    )
    name = forms.CharField(
        max_length=ITEM_NAME_MAX_LENGTH,
        widget=forms.TextInput(attrs={"class": "input", "placeholder": "JDK 17"}),
    )
    kind = forms.ChoiceField(
        choices=ArtifactKind.choices,
        initial=ArtifactKind.OTHER,
        widget=forms.Select(attrs={"class": "select"}),
    )
    path_pattern = forms.CharField(
        required=False,
        max_length=ITEM_PATH_PATTERN_MAX_LENGTH,
        widget=forms.TextInput(
            attrs={
                "class": "input",
                "placeholder": "installers/jdk-17*.msi",
                "spellcheck": "false",
            }
        ),
    )
    expected_version = forms.CharField(
        required=False,
        max_length=ITEM_EXPECTED_VERSION_MAX_LENGTH,
        widget=forms.TextInput(
            attrs={"class": "input", "placeholder": "17.0.2 or latest"}
        ),
    )
    source = forms.CharField(
        required=False,
        max_length=ITEM_SOURCE_MAX_LENGTH,
        widget=forms.TextInput(
            attrs={"class": "input", "placeholder": "TeamCity › Desktop › Release"}
        ),
    )
    install_hint = forms.CharField(
        required=False,
        max_length=INSTALL_HINT_MAX_LENGTH,
        widget=forms.Textarea(attrs={"class": "textarea", "rows": 2}),
    )
    knowledge_item_id = forms.IntegerField(
        required=False,
        min_value=1,
        widget=forms.NumberInput(attrs={"class": "input", "placeholder": "e.g. 12"}),
    )

    def clean_key(self) -> str:
        key: str = self.cleaned_data["key"]
        problem = key_error(key)
        if problem:
            raise forms.ValidationError(problem, code="key")
        return key

    def clean_path_pattern(self) -> str:
        pattern: str = self.cleaned_data["path_pattern"]
        problem = glob_error(pattern)
        if problem:
            raise forms.ValidationError(problem[:1].upper() + problem[1:], code="glob")
        return pattern

    def to_item(self) -> ProfileItemData:
        data = self.cleaned_data
        kb_id = data.get("knowledge_item_id")
        return ProfileItemData(
            key=data["key"],
            name=data["name"],
            kind=data["kind"],
            path_pattern=data["path_pattern"],
            expected_version=data["expected_version"],
            source=data["source"],
            install_hint=data["install_hint"],
            knowledge_item_id=kb_id if isinstance(kb_id, int) else None,
        )


class BaseArtifactItemFormSet(BaseFormSet[ArtifactItemForm]):
    def clean(self) -> None:
        super().clean()
        if any(self.errors):
            return
        seen: dict[str, ArtifactItemForm] = {}
        for form in self._kept_forms():
            key = form.cleaned_data["key"]
            if key in seen:
                form.add_error("key", f"Key «{key}» is used by another artifact.")
            seen[key] = form

    def _kept_forms(self) -> list[ArtifactItemForm]:
        """Saved rows and filled new rows not marked for deletion, in page order."""
        initial_count = self.initial_form_count()
        return [
            form
            for index, form in enumerate(self.forms)
            if (index < initial_count or form.has_changed())
            and form.cleaned_data
            and not form.cleaned_data.get("DELETE", False)
        ]

    def items(self) -> tuple[ProfileItemData, ...]:
        """The rows to save, in page order (call after ``is_valid``)."""
        return tuple(form.to_item() for form in self._kept_forms())

    def forms_citing(self, knowledge_item_id: int) -> list[ArtifactItemForm]:
        return [
            form
            for form in self._kept_forms()
            if form.cleaned_data.get("knowledge_item_id") == knowledge_item_id
        ]


# formset_factory is typed as returning type[BaseFormSet[F]]; it returns a
# subclass of the given ``formset``, so the cast only restores that class.
ArtifactItemFormSet = cast(
    type[BaseArtifactItemFormSet],
    formset_factory(
        ArtifactItemForm,
        formset=BaseArtifactItemFormSet,
        extra=ITEM_FORMSET_EXTRA,
        can_delete=True,
        max_num=PROFILE_MAX_ITEMS,
        validate_max=True,
    ),
)


def profile_form_initial(data: ProfileData, *, revision: int) -> dict[str, object]:
    return {
        "base_dir_windows": data.base_dir_windows,
        "base_dir_macos": data.base_dir_macos,
        "base_dir_linux": data.base_dir_linux,
        "notes": data.notes,
        "change_summary": "",
        "base_revision": revision,
    }


def item_formset_initial(data: ProfileData) -> list[dict[str, object]]:
    return [
        {
            "key": item.key,
            "name": item.name,
            "kind": item.kind,
            "path_pattern": item.path_pattern,
            "expected_version": item.expected_version,
            "source": item.source,
            "install_hint": item.install_hint,
            "knowledge_item_id": item.knowledge_item_id,
        }
        for item in data.items
    ]


def profile_data_from_forms(
    form: ArtifactProfileForm, formset: BaseArtifactItemFormSet
) -> ProfileData:
    """The ``ProfileData`` both valid forms describe."""
    return ProfileData(
        base_dir_windows=form.cleaned_data["base_dir_windows"],
        base_dir_macos=form.cleaned_data["base_dir_macos"],
        base_dir_linux=form.cleaned_data["base_dir_linux"],
        notes=form.cleaned_data["notes"],
        items=formset.items(),
    )


# The hidden ``action`` of a requirement row's POST: which form it carries.
REQUIREMENT_ACTION_EDIT = "edit"
REQUIREMENT_ACTION_STATUS = "status"


class Stage0RequirementForm(forms.Form):
    """A person's edit of a requirement's definition (key and conflicts kept)."""

    title = forms.CharField(
        max_length=REQUIREMENT_TITLE_MAX_LENGTH,
        widget=forms.TextInput(attrs={"class": "input"}),
    )
    kind = forms.ChoiceField(
        choices=RequirementKind.choices,
        widget=forms.Select(attrs={"class": "select"}),
    )
    check_target = forms.CharField(
        required=False,
        max_length=REQUIREMENT_CHECK_TARGET_MAX_LENGTH,
        widget=forms.TextInput(
            attrs={
                "class": "input",
                "placeholder": "App name, path or artifact key",
                "spellcheck": "false",
            }
        ),
    )
    install_hint = forms.CharField(
        required=False,
        max_length=INSTALL_HINT_MAX_LENGTH,
        widget=forms.Textarea(attrs={"class": "textarea", "rows": 2}),
    )
    ignored = forms.BooleanField(required=False)


class RequirementStatusForm(forms.Form):
    """A person's status for one requirement, with optional evidence."""

    status = forms.ChoiceField(
        choices=RequirementStatus.choices,
        widget=forms.Select(attrs={"class": "select"}),
    )
    detail = forms.CharField(
        required=False,
        max_length=REQUIREMENT_STATUS_DETAIL_MAX_LENGTH,
        widget=forms.TextInput(
            attrs={"class": "input", "placeholder": "How you know (optional)"}
        ),
    )
