"""Machine readiness tab: thin views over the machine_readiness services."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from accounts.types import AuthenticatedRequest
from auto_tester.celery import app as celery_app
from auto_tester.pagination import elided_page_range, parse_page
from machine_readiness.exceptions import (
    ArtifactKnowledgeRefError,
    ArtifactProfileConflictError,
    ArtifactProfileInvalidError,
    ArtifactProfileVersionNotFoundError,
    MachineReadinessError,
    RequirementNotFoundError,
)
from machine_readiness.forms import (
    REQUIREMENT_ACTION_EDIT,
    REQUIREMENT_ACTION_STATUS,
    ArtifactItemForm,
    ArtifactItemFormSet,
    ArtifactProfileForm,
    BaseArtifactItemFormSet,
    RequirementStatusForm,
    Stage0RequirementForm,
    item_formset_initial,
    profile_data_from_forms,
    profile_form_initial,
)
from machine_readiness.models import PROFILE_MAX_ITEMS, ProfileWriteSource
from machine_readiness.services.analyse_preconditions import analysis_running
from machine_readiness.services.artifact_profile import get_profile, write_profile
from machine_readiness.services.gap_report import build_gap_report
from machine_readiness.services.profile_overview import (
    get_profile_overview,
    kb_labels_for,
)
from machine_readiness.services.requirement_list import get_requirements_page
from machine_readiness.services.requirement_status import (
    edit_requirement,
    set_requirement_status,
)
from machine_readiness.services.restore_profile_version import (
    restore_profile_version,
)
from machine_readiness.services.scan_artifacts import runner_host, scan_availability
from machine_readiness.services.skip_intake import skip_intake
from machine_readiness.task_names import ANALYSE_PRECONDITIONS, SCAN_ARTIFACTS
from projects.decorators import project_membership_required
from projects.models import Project

ITEM_FORMSET_PREFIX = "items"


@project_membership_required
@require_GET
def overview(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    data = get_profile_overview(project.id)
    scan = scan_availability(project.id)
    gap_report = build_gap_report(project_id=project.id, run_id=None)
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
            "inventory": gap_report.inventory,
            "matches": gap_report.match_rows,
            "runner_os": scan.runner_os_label,
            "scan_available": scan.available,
            "scan_reason": scan.reason,
            "gap_report": gap_report,
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


@project_membership_required
@require_POST
def scan(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    availability = scan_availability(project.id)
    if not availability.available:
        messages.error(request, availability.reason)
    else:
        celery_app.send_task(SCAN_ARTIFACTS, args=[project.id, request.user.id])
        messages.success(
            request,
            f"Scanning the {availability.runner_os_label} base directory. "
            "Reload this page in a few seconds to see the inventory.",
        )
    return redirect("machine_readiness:overview", project_id=project.id)


@project_membership_required
@require_GET
def requirements(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    data = get_requirements_page(project.id, page=parse_page(request))
    rows = [
        (
            requirement,
            Stage0RequirementForm(
                initial={
                    "title": requirement.title,
                    "kind": requirement.kind,
                    "check_target": requirement.check_target,
                    "install_hint": requirement.install_hint,
                    "ignored": requirement.ignored,
                },
                auto_id=f"id_req{requirement.pk}_%s",
            ),
            RequirementStatusForm(
                initial={"status": requirement.status, "detail": ""},
                auto_id=f"id_req{requirement.pk}_status_%s",
            ),
        )
        for requirement in data.requirements
    ]
    return render(
        request,
        "machine_readiness/requirements.html",
        {
            "project": project,
            "active_tab": "machine_readiness",
            "groups": data.groups,
            "requirements": data.requirements,
            "requirement_rows": rows,
            "page_obj": data.requirements,
            "elided_page_range": elided_page_range(data.requirements),
            "unanalysed_count": data.unanalysed_count,
            "analysis_running": data.analysis_running,
            "action_edit": REQUIREMENT_ACTION_EDIT,
            "action_status": REQUIREMENT_ACTION_STATUS,
        },
    )


@project_membership_required
@require_POST
def requirements_analyse(
    request: AuthenticatedRequest, project: Project
) -> HttpResponse:
    if analysis_running(project.id):
        messages.info(
            request,
            "The preconditions are being analysed already; reload this page in "
            "a few minutes.",
        )
    else:
        celery_app.send_task(ANALYSE_PRECONDITIONS, args=[project.id])
        messages.success(
            request,
            "Analysing the preconditions. This takes a few minutes; reload this "
            "page to see the requirements.",
        )
    return redirect("machine_readiness:requirements", project_id=project.id)


@project_membership_required
@require_POST
def requirement_update(
    request: AuthenticatedRequest, project: Project, requirement_id: int
) -> HttpResponse:
    action = request.POST.get("action", "")
    try:
        if action == REQUIREMENT_ACTION_EDIT:
            edit_form = Stage0RequirementForm(request.POST)
            if not edit_form.is_valid():
                _form_errors(request, edit_form)
            else:
                edit_requirement(
                    project_id=project.id,
                    requirement_id=requirement_id,
                    title=edit_form.cleaned_data["title"],
                    kind=edit_form.cleaned_data["kind"],
                    check_target=edit_form.cleaned_data["check_target"],
                    install_hint=edit_form.cleaned_data["install_hint"],
                    ignored=edit_form.cleaned_data["ignored"],
                )
                messages.success(request, "Requirement saved.")
        elif action == REQUIREMENT_ACTION_STATUS:
            status_form = RequirementStatusForm(request.POST)
            if not status_form.is_valid():
                _form_errors(request, status_form)
            else:
                set_requirement_status(
                    project_id=project.id,
                    requirement_id=requirement_id,
                    status=status_form.cleaned_data["status"],
                    detail=status_form.cleaned_data["detail"],
                    author=request.user,
                    host=runner_host(project.agent_system_info),
                )
                messages.success(request, "Status recorded.")
        else:
            return HttpResponseBadRequest("Unknown action.")
    except RequirementNotFoundError as exc:
        raise Http404(exc.message) from exc
    except MachineReadinessError as exc:
        messages.error(request, exc.message)
    url = reverse("machine_readiness:requirements", kwargs={"project_id": project.id})
    page = request.POST.get("page", "")
    query = f"?page={int(page)}" if page.isdigit() and int(page) > 1 else ""
    return redirect(f"{url}{query}#req-{requirement_id}")


def _form_errors(request: AuthenticatedRequest, form: forms.Form) -> None:
    """Each error of an invalid row form as a message (the page is PRG)."""
    for name, errors in form.errors.items():
        field = form.fields.get(name)
        label = (field.label if field is not None else "") or name.replace(
            "_", " "
        ).strip().capitalize()
        for error in errors:
            messages.error(request, f"{label}: {error}" if field else str(error))


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
