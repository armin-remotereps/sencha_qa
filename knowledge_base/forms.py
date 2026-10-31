"""Forms of the Knowledge base tab (search, tag filter, upload, edit)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from django import forms
from django.conf import settings
from django.core.files.uploadedfile import UploadedFile

from knowledge_base.formats import accept_attribute
from knowledge_base.models import (
    KNOWLEDGE_DESCRIPTION_MAX,
    KNOWLEDGE_FOLDER_MAX_LENGTH,
    KNOWLEDGE_MAX_TAGS_PER_ITEM,
)
from knowledge_base.services.search_knowledge import SEARCH_QUERY_MAX_LENGTH
from knowledge_base.tags import parse_tag_input

KNOWLEDGE_ITEM_NAME_MAX_LENGTH = 120
# Free text the tags field accepts before parsing (20 tags of 64 characters).
KNOWLEDGE_TAGS_INPUT_MAX_LENGTH = 2000


class KnowledgeTagsField(forms.CharField):
    """Comma- or newline-separated manual tags; cleans to a normalised tuple."""

    def __init__(self) -> None:
        super().__init__(
            required=False,
            max_length=KNOWLEDGE_TAGS_INPUT_MAX_LENGTH,
            widget=forms.TextInput(
                attrs={"class": "input", "placeholder": "release-2.3, windows"}
            ),
        )

    def clean(self, value: object) -> tuple[str, ...]:
        text: str = super().clean(value)
        tags = parse_tag_input(text)
        if len(tags) > KNOWLEDGE_MAX_TAGS_PER_ITEM:
            raise forms.ValidationError(
                f"At most {KNOWLEDGE_MAX_TAGS_PER_ITEM} tags.", code="too_many_tags"
            )
        return tags


class KnowledgeSearchForm(forms.Form):
    """GET search box; ``exact`` switches to an exact-substring match."""

    q = forms.CharField(
        max_length=SEARCH_QUERY_MAX_LENGTH,
        required=False,
        strip=True,
        widget=forms.TextInput(
            attrs={
                "class": "input",
                "type": "search",
                "placeholder": "Search the knowledge base…",
            }
        ),
    )
    exact = forms.BooleanField(required=False)
    # A manual tag or an archive folder path (its subfolders are included).
    tag = forms.CharField(
        max_length=KNOWLEDGE_FOLDER_MAX_LENGTH, required=False, strip=True
    )


class MultipleFileInput(forms.ClearableFileInput):
    """Django's documented multi-file widget."""

    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """Django's documented multi-file field: cleans to a list of files."""

    def clean(
        self, data: object, initial: object = None
    ) -> list[UploadedFile] | UploadedFile | None:
        single_file_clean = super().clean
        if isinstance(data, (list, tuple)):
            return [single_file_clean(item, initial) for item in data]
        single: UploadedFile | None = single_file_clean(data, initial)
        return single


class KnowledgeUploadForm(forms.Form):
    """Several files plus an optional description and tags applied to each."""

    # Not named "files": that would shadow BaseForm.files (request.FILES).
    uploads = MultipleFileField(
        widget=MultipleFileInput(attrs={"class": "input"}),
        error_messages={"required": "Choose at least one file."},
    )
    description = forms.CharField(
        max_length=KNOWLEDGE_DESCRIPTION_MAX,
        required=False,
        strip=True,
        widget=forms.Textarea(attrs={"class": "textarea", "rows": 2}),
    )
    tags = KnowledgeTagsField()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["uploads"].widget.attrs["accept"] = accept_attribute(
            accept_7z=settings.KNOWLEDGE_BASE_ACCEPT_7Z
        )

    def cleaned_files(self) -> Sequence[UploadedFile]:
        value = self.cleaned_data["uploads"]
        if value is None:
            return ()
        if isinstance(value, list):
            return value
        return (value,)


class KnowledgeItemEditForm(forms.Form):
    """Rename, describe and tag an item."""

    name = forms.CharField(
        max_length=KNOWLEDGE_ITEM_NAME_MAX_LENGTH,
        strip=True,
        widget=forms.TextInput(attrs={"class": "input"}),
    )
    description = forms.CharField(
        max_length=KNOWLEDGE_DESCRIPTION_MAX,
        required=False,
        strip=True,
        widget=forms.Textarea(attrs={"class": "textarea", "rows": 3}),
    )
    tags = KnowledgeTagsField()
