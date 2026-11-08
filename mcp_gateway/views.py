from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from accounts.types import AuthenticatedRequest
from mcp_gateway.exceptions import (
    ConsentProjectRefused,
    ConsentUnavailable,
    GrantNotFound,
    GrantProjectsRefused,
)
from mcp_gateway.forms import ConsentDecision, ConsentForm, GrantProjectsForm
from mcp_gateway.limits import MCP_OAUTH_CONSENT_PER_MINUTE
from mcp_gateway.oauth_policy import canonical_resource, oauth_enabled
from mcp_gateway.services.oauth_access import writable_projects
from mcp_gateway.services.oauth_connections import list_connections
from mcp_gateway.services.oauth_consent import (
    ConsentView,
    approve,
    deny,
    load_consent,
)
from mcp_gateway.services.oauth_grant_projects import (
    GrantProjects,
    load_grant_projects,
    set_grant_projects,
)
from mcp_gateway.services.oauth_revoke import revoke_grant_for_user
from projects.runner_person_step_services import hit_runner_throttle

SCOPE_TEXT = (
    "Claim and perform Manual run cases, send screenshots, hand in results, "
    "and read runs and the knowledge base of the chosen projects."
)
_CONSENT_WINDOW_SECONDS = 60
_EDIT_THROTTLED = "Too many changes in a minute. Wait a minute, then try again."


def _render_consent(
    request: AuthenticatedRequest,
    handle: str,
    consent: ConsentView,
    form: ConsentForm,
    *,
    error_message: str = "",
    status: int = 200,
) -> HttpResponse:
    return render(
        request,
        "mcp_gateway/consent.html",
        {
            "handle": handle,
            "client_display_name": consent.client_display_name,
            "client_declared_name": consent.client_declared_name,
            "redirect_host": consent.redirect_host,
            "redirect_uri": consent.redirect_uri,
            "is_loopback": consent.is_loopback,
            "projects": consent.projects,
            "form": form,
            "scope_text": SCOPE_TEXT,
            "error_message": error_message,
        },
        status=status,
    )


def _render_unavailable(request: AuthenticatedRequest) -> HttpResponse:
    # Never redirect here: an unknown handle gives no trusted place to go.
    return render(request, "mcp_gateway/consent_unavailable.html", status=400)


@login_required
@require_http_methods(["GET", "POST"])
def consent(request: AuthenticatedRequest, handle: str) -> HttpResponse:
    if request.method == "POST" and not hit_runner_throttle(
        f"mcp_gateway:consent:{request.user.pk}",
        limit=MCP_OAUTH_CONSENT_PER_MINUTE,
        window_seconds=_CONSENT_WINDOW_SECONDS,
    ):
        return render(request, "mcp_gateway/consent_throttled.html", status=429)
    try:
        view = load_consent(handle, request.user)
    except ConsentUnavailable:
        return _render_unavailable(request)

    if request.method != "POST":
        return _render_consent(request, handle, view, ConsentForm(view.projects))

    form = ConsentForm(view.projects, request.POST)
    if not form.is_valid():
        return _render_consent(request, handle, view, form, status=400)
    try:
        if form.cleaned_data["decision"] == ConsentDecision.DENY:
            url = deny(handle)
        else:
            url = approve(handle, request.user, form.cleaned_data["projects"])
    except ConsentUnavailable:
        return _render_unavailable(request)
    except ConsentProjectRefused as exc:
        return _render_consent(
            request, handle, view, form, error_message=exc.message, status=400
        )
    # The redirect URI matched the client's registration, which only allows
    # https to an allowlisted host or loopback http.
    return HttpResponseRedirect(url)


@login_required
@require_GET
def connections(request: AuthenticatedRequest) -> HttpResponse:
    return render(
        request,
        "mcp_gateway/connections.html",
        {
            "active_nav": "connected_apps",
            "connections": list_connections(request.user),
            "mcp_url": canonical_resource(),
            "oauth_enabled": oauth_enabled(),
        },
    )


def _render_edit_projects(
    request: AuthenticatedRequest,
    grant: GrantProjects,
    form: GrantProjectsForm,
    *,
    error_message: str = "",
    status: int = 200,
) -> HttpResponse:
    return render(
        request,
        "mcp_gateway/edit_projects.html",
        {
            "active_nav": "connected_apps",
            "grant": grant,
            "form": form,
            "error_message": error_message,
        },
        status=status,
    )


@login_required
@require_http_methods(["GET", "POST"])
def edit_projects(request: AuthenticatedRequest, grant_id: int) -> HttpResponse:
    try:
        grant = load_grant_projects(request.user, grant_id)
    except GrantNotFound as exc:
        raise Http404(exc.message) from exc
    projects = writable_projects(request.user)

    if request.method != "POST":
        form = GrantProjectsForm(projects, selected_ids=grant.project_ids)
        return _render_edit_projects(request, grant, form)
    # Shares the consent Allow budget: both write connection projects.
    if not hit_runner_throttle(
        f"mcp_gateway:consent:{request.user.pk}",
        limit=MCP_OAUTH_CONSENT_PER_MINUTE,
        window_seconds=_CONSENT_WINDOW_SECONDS,
    ):
        return _render_edit_projects(
            request,
            grant,
            GrantProjectsForm(projects, selected_ids=grant.project_ids),
            error_message=_EDIT_THROTTLED,
            status=429,
        )

    form = GrantProjectsForm(projects, request.POST)
    if not form.is_valid():
        return _render_edit_projects(request, grant, form, status=400)
    try:
        set_grant_projects(request.user, grant_id, form.cleaned_data["projects"])
    except GrantNotFound as exc:
        raise Http404(exc.message) from exc
    except GrantProjectsRefused as exc:
        return _render_edit_projects(
            request, grant, form, error_message=exc.message, status=400
        )
    messages.success(request, "Connection updated.")
    return redirect("mcp_gateway:connections")


@login_required
@require_POST
def revoke(request: AuthenticatedRequest, grant_id: int) -> HttpResponse:
    try:
        revoke_grant_for_user(grant_id, request.user.pk)
    except GrantNotFound as exc:
        raise Http404(exc.message) from exc
    messages.success(
        request, "Connection revoked. The app can no longer act on its projects."
    )
    return redirect("mcp_gateway:connections")
