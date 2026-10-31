"""Machine readiness tab: thin views over the machine_readiness services."""

from __future__ import annotations

from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from accounts.types import AuthenticatedRequest
from machine_readiness.exceptions import (
    ArtifactKnowledgeRefError,
    ArtifactProfileConflictError,
    ArtifactProfileInvalidError,
    ArtifactProfileVersionNotFoundError,
)
from machine_readiness.forms import (
    ArtifactItemForm,
    ArtifactItemFormSet,
    ArtifactProfileForm,
    BaseArtifactItemFormSet,
    item_formset_initial,
    profile_data_from_forms,
    profile_form_initial,
)
from machine_readiness.models import PROFILE_MAX_ITEMS, ProfileWriteSource
from machine_readiness.services.artifact_profile import get_profile, write_profile
from machine_readiness.services.profile_overview import (
    get_profile_overview,
    kb_labels_for,
)
from machine_readiness.services.restore_profile_version import (
    restore_profile_version,
)
from machine_readiness.services.skip_intake import skip_intake
from projects.decorators import project_membership_required
from projects.models import Project

ITEM_FORMSET_PREFIX = "items"


@project_membership_required
@require_GET
def overview(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    data = get_profile_overview(project.id)
    return render(
        request,
        "machine_readiness/overview.html",
        {
            "project": project,
            "active_tab": "machine_readiness",
            "profile": data.profile,
            "items": data.items,
            "provenance": data.provenance,
            "versions": data.versions,
            "intake_pending": data.intake_pending,
            # Filled by the scan and gap report step; None renders the
            # "coming" empty state in the Inventory and Gaps sections.
            "inventory": None,
            "matches": None,
            "runner_os": None,
            "scan_available": False,
            "gap_report": None,
        },
    )


@project_membership_required
@require_http_methods(["GET", "POST"])
def artifacts_edit(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    if request.method != "POST":
        return _render_current(request, project)

    form = ArtifactProfileForm(request.POST)
    formset = ArtifactItemFormSet(request.POST, prefix=ITEM_FORMSET_PREFIX)
    if not (form.is_valid() and formset.is_valid()):
        return _render_edit(request, project, form, formset, status=400)
    try:
        revision = write_profile(
            project_id=project.id,
            data=profile_data_from_forms(form, formset),
            source=ProfileWriteSource.USER,
            author=request.user,
            change_summary=form.cleaned_data["change_summary"],
            expected_revision=form.cleaned_data["base_revision"],
        )
    except ArtifactProfileConflictError as exc:
        messages.error(
            request,
            f"{exc.message} The form now shows the current profile; "
            "make your changes again.",
        )
        return _render_current(request, project, status=409)
    except ArtifactKnowledgeRefError as exc:
        for item_id in exc.item_ids:
            for item_form in formset.forms_citing(item_id):
                item_form.add_error(
                    "knowledge_item_id",
                    f"Knowledge base item #{item_id} does not exist in this project.",
                )
        return _render_edit(request, project, form, formset, status=400)
    except ArtifactProfileInvalidError as exc:
        for error in exc.errors:
            form.add_error(None, error)
        return _render_edit(request, project, form, formset, status=400)
    messages.success(request, f"Artifacts profile saved (revision {revision}).")
    return redirect("machine_readiness:overview", project_id=project.id)


@project_membership_required
@require_POST
def artifacts_version_restore(
    request: AuthenticatedRequest, project: Project, version_id: int
) -> HttpResponse:
    try:
        revision = restore_profile_version(
            project_id=project.id, version_id=version_id, author=request.user
        )
    except ArtifactProfileVersionNotFoundError as exc:
        raise Http404(exc.message) from exc
    except ArtifactProfileInvalidError as exc:
        messages.error(request, f"This version cannot be restored: {exc.message}")
    else:
        messages.success(request, f"Version restored as revision {revision}.")
    return redirect("machine_readiness:overview", project_id=project.id)


@project_membership_required
@require_POST
def intake_skip(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    skip_intake(project_id=project.id, author=request.user)
    messages.success(
        request,
        "Intake skipped. Machine setup can work on the machine without an "
        "artifacts profile; you can still fill it in on this tab.",
    )
    next_url = request.POST.get("next", "")
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect("machine_readiness:overview", project_id=project.id)


def _render_current(
    request: AuthenticatedRequest, project: Project, *, status: int = 200
) -> HttpResponse:
    """The edit page filled with the saved profile."""
    snapshot = get_profile(project.id)
    form = ArtifactProfileForm(
        initial=profile_form_initial(snapshot.data, revision=snapshot.revision)
    )
    formset = ArtifactItemFormSet(
        initial=item_formset_initial(snapshot.data), prefix=ITEM_FORMSET_PREFIX
    )
    return _render_edit(request, project, form, formset, status=status)


def _render_edit(
    request: AuthenticatedRequest,
    project: Project,
    form: ArtifactProfileForm,
    formset: BaseArtifactItemFormSet,
    *,
    status: int = 200,
) -> HttpResponse:
    cited = [_cited_id(item_form) for item_form in formset.forms]
    # Shown beside each KB id field; a deleted item reads "KB item #N (deleted)".
    labels = kb_labels_for(
        project.id, [item_id for item_id in cited if item_id is not None]
    )
    return render(
        request,
        "machine_readiness/artifacts_edit.html",
        {
            "project": project,
            "active_tab": "machine_readiness",
            "form": form,
            "formset": formset,
            "item_rows": [
                (item_form, labels.get(item_id, "") if item_id is not None else "")
                for item_form, item_id in zip(formset.forms, cited, strict=True)
            ],
            "max_items": PROFILE_MAX_ITEMS,
        },
        status=status,
    )


def _cited_id(item_form: ArtifactItemForm) -> int | None:
    """The KB id a row shows (bound or initial value), when it is a number."""
    value = item_form["knowledge_item_id"].value()
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
        return number if number > 0 else None
    return None
