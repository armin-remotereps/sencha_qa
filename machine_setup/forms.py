from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from django import forms
from django.conf import settings
from django.core.files.uploadedfile import UploadedFile
from django.http import QueryDict
from django.utils.http import url_has_allowed_host_and_scheme

from auto_tester.file_names import sanitize_file_name
from knowledge_base.reference_note import MAX_KNOWLEDGE_REFS_PER_MESSAGE
from machine_setup.document_text import (
    UploadedDocument,
    decode_document,
    has_document_extension,
)
from machine_setup.exceptions import SetupDocumentInvalidError

SETUP_MESSAGE_MAX_LENGTH = 8000
DOCUMENT_TYPE_MESSAGE = "Attach a Markdown or plain text file (.md, .markdown, .txt)."
DOCUMENT_THREAD_MESSAGE = "Documents can be attached in the main chat only."
EMPTY_MESSAGE = "Write a message, attach a document or pick a knowledge base item."
KNOWLEDGE_REFS_INVALID_MESSAGE = "Pick knowledge base items from the list."
KNOWLEDGE_REFS_TOO_MANY_MESSAGE = (
    f"Pick at most {MAX_KNOWLEDGE_REFS_PER_MESSAGE} knowledge base items."
)


class KnowledgeItemIdsField(forms.Field):
    """Repeated ``knowledge_item_ids`` values: positive ints, deduplicated in
    order, at most ``MAX_KNOWLEDGE_REFS_PER_MESSAGE``. Whether each id is an
    item of the project is checked by the send service."""

    widget = forms.MultipleHiddenInput

    def __init__(self) -> None:
        super().__init__(required=False)

    def to_python(self, value: object) -> list[int]:
        if not value:
            return []
        if not isinstance(value, (list, tuple)):
            raise forms.ValidationError(KNOWLEDGE_REFS_INVALID_MESSAGE)
        ids: list[int] = []
        for raw in value:
            if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit():
                raise forms.ValidationError(KNOWLEDGE_REFS_INVALID_MESSAGE)
            item_id = int(raw)
            if item_id < 1:
                raise forms.ValidationError(KNOWLEDGE_REFS_INVALID_MESSAGE)
            ids.append(item_id)
        unique = list(dict.fromkeys(ids))
        if len(unique) > MAX_KNOWLEDGE_REFS_PER_MESSAGE:
            raise forms.ValidationError(KNOWLEDGE_REFS_TOO_MANY_MESSAGE)
        return unique


class SetupMessageForm(forms.Form):
    """One chat message to the machine setup agent."""

    text = forms.CharField(
        max_length=SETUP_MESSAGE_MAX_LENGTH,
        strip=True,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    # The thread replied in; empty for the main chat.
    conversation_id = forms.IntegerField(
        required=False, min_value=1, widget=forms.HiddenInput
    )
    # Main chat only: one Markdown/text file.
    document = forms.FileField(required=False)
    # Any thread: knowledge base items the message points the agent at.
    knowledge_item_ids = KnowledgeItemIdsField()

    def clean_document(self) -> UploadedDocument | None:
        upload: UploadedFile | None = self.cleaned_data.get("document")
        if not upload:
            return None
        name = upload.name or ""
        if not has_document_extension(name):
            raise forms.ValidationError(DOCUMENT_TYPE_MESSAGE)
        max_bytes: int = settings.MACHINE_SETUP_DOCUMENT_MAX_BYTES
        too_big = forms.ValidationError(
            f"The document is larger than {max_bytes // 1024:,} KB."
        )
        # The declared size is checked before reading anything.
        if upload.size is None or upload.size > max_bytes:
            raise too_big
        raw = upload.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise too_big
        try:
            text = decode_document(raw)
        except SetupDocumentInvalidError as exc:
            raise forms.ValidationError(exc.message) from exc
        return UploadedDocument(
            file_name=sanitize_file_name(name), text=text, size_bytes=len(raw)
        )

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        if "document" in self.errors:
            return cleaned
        document = cleaned.get("document")
        if document is not None and cleaned.get("conversation_id") is not None:
            raise forms.ValidationError(DOCUMENT_THREAD_MESSAGE)
        if "knowledge_item_ids" in self.errors:
            return cleaned
        if (
            document is None
            and not cleaned.get("knowledge_item_ids")
            and not (cleaned.get("text") or "").strip()
        ):
            raise forms.ValidationError(EMPTY_MESSAGE)
        return cleaned

    def uploaded_document(self) -> UploadedDocument | None:
        """The validated document; call after ``is_valid()``."""
        document = self.cleaned_data.get("document")
        return document if isinstance(document, UploadedDocument) else None

    def referenced_item_ids(self) -> list[int]:
        """The validated, deduplicated item ids; call after ``is_valid()``."""
        ids: list[int] = self.cleaned_data.get("knowledge_item_ids") or []
        return ids


class SetupThreadForm(forms.Form):
    """The thread a Stop was pressed in, so the page returns to it."""

    conversation_id = forms.IntegerField(
        required=False, min_value=1, widget=forms.HiddenInput
    )


class SetupForceStopForm(forms.Form):
    """The running turn to force stop, and the thread to return to."""

    turn_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    conversation_id = forms.IntegerField(
        required=False, min_value=1, widget=forms.HiddenInput
    )


class OpenHelpChatForm(forms.Form):
    """Open the help chat of a case that needs help, with an optional first message."""

    text = forms.CharField(
        label="First message",
        max_length=SETUP_MESSAGE_MAX_LENGTH,
        strip=True,
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "textarea",
                "rows": 3,
                "placeholder": (
                    "Optional: what you want to talk about. Empty asks the agent "
                    "to look at the machine and suggest what to do."
                ),
            }
        ),
    )


class OpenResultChatForm(forms.Form):
    """Open the result chat of a finished case, with an optional first message."""

    text = forms.CharField(
        label="First message",
        max_length=SETUP_MESSAGE_MAX_LENGTH,
        strip=True,
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "textarea",
                "rows": 3,
                "placeholder": (
                    "Optional: what looks wrong about this result. Empty asks "
                    "the agent to review it with the evidence."
                ),
            }
        ),
    )


class OpenManualChatForm(forms.Form):
    """Open the discussion of a claimed Manual run case, with an optional message."""

    text = forms.CharField(
        label="Message",
        max_length=SETUP_MESSAGE_MAX_LENGTH,
        strip=True,
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "textarea",
                "rows": 3,
                "placeholder": (
                    "Optional: what you want to ask about this case. Empty just "
                    "opens the discussion."
                ),
            }
        ),
    )


class OpenTriageChatForm(forms.Form):
    """Open (or re-open) a run's case analysis thread, with an optional message.

    Posted from the run page's Case analysis card and the Machine setup
    page's "Start an analysis" picker. ``next`` is where a refusal returns
    to; an unsafe or foreign URL is dropped (cleaned to "").
    """

    test_run_id = forms.IntegerField(min_value=1)
    text = forms.CharField(
        label="Message",
        max_length=SETUP_MESSAGE_MAX_LENGTH,
        strip=True,
        required=False,
    )
    next = forms.CharField(required=False, max_length=2000, widget=forms.HiddenInput)

    def __init__(
        self,
        data: QueryDict | None = None,
        *,
        allowed_hosts: set[str],
        require_https: bool,
    ) -> None:
        super().__init__(data)
        self._allowed_hosts = allowed_hosts
        self._require_https = require_https

    def clean_next(self) -> str:
        next_url: str = self.cleaned_data.get("next") or ""
        if next_url and url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts=self._allowed_hosts,
            require_https=self._require_https,
        ):
            return next_url
        return ""


class SetupRunnerSelectForm(forms.Form):
    """The main chat header's Test Runner picker.

    ``runners`` are the member's usable runners for the project
    (``runner_access.usable_runners``); the empty choice clears the pick.
    The service re-checks ownership under the runner lock.
    """

    runner = forms.TypedChoiceField(
        label="Test Runner",
        required=False,
        coerce=int,
        empty_value=None,
    )

    def __init__(
        self,
        *args: Any,
        runners: Sequence[tuple[int, str]] = (),
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        field = self.fields["runner"]
        assert isinstance(field, forms.TypedChoiceField)
        field.choices = [("", "No Test Runner"), *runners]
