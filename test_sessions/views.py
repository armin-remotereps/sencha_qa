"""Test session pages (``/test-sessions/``): list, start, detail, stop, edit,
delete and the CSV export.

Thin views: bind a form, call a service, map ``TestSessionError`` subclasses
to a message and a redirect (``SessionNotFoundError`` to 404). Every service
filters on the requesting user, so another user's session id is a 404.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, StreamingHttpResponse
from django.http.response import HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST, require_safe

from accounts.types import AuthenticatedRequest
from auto_tester.pagination import (
    ALLOWED_PER_PAGE,
    elided_page_range,
    parse_page,
    parse_per_page,
)
from test_sessions.exceptions import (
    SessionAlreadyClosedError,
    SessionAlreadyOpenError,
    SessionExportThrottledError,
    SessionNotFoundError,
)
from test_sessions.forms import (
    TestSessionEditForm,
    TestSessionStartForm,
    TestSessionStopForm,
)
from test_sessions.services.delete_session import delete_session
from test_sessions.services.edit_session import edit_session
from test_sessions.services.export_throttle import check_session_export_throttle
from test_sessions.services.session_export import stream_session_csv
from test_sessions.services.session_queries import (
    get_open_session,
    get_session_for_user,
    get_session_summary,
    list_session_cases,
    list_sessions,
)
from test_sessions.services.start_session import start_session
from test_sessions.services.stop_session import stop_session


def _form_errors(form: forms.Form) -> str:
    """A form's errors as one line for ``messages`` ("Label: error ...")."""
    parts: list[str] = []
    for field_name, errors in form.errors.items():
        field = form.fields.get(field_name)
        label = str(field.label) if field is not None and field.label else ""
        parts.extend(f"{label}: {error}" if label else str(error) for error in errors)
    return " ".join(parts)


def _detail_redirect(session_id: int) -> HttpResponse:
    return redirect("test_sessions:detail", session_id=session_id)


@login_required
@require_safe
def session_list(request: AuthenticatedRequest) -> HttpResponse:
    open_session = get_open_session(request.user)
    per_page = parse_per_page(request)
    page_obj = list_sessions(request.user, page=parse_page(request), per_page=per_page)
    context: dict[str, object] = {
        "open_session": open_session,
        # Reused by the nav chip, so the context processor makes no query.
        "open_test_session": open_session,
        "start_form": TestSessionStartForm(),
        "page_obj": page_obj,
        "elided_page_range": elided_page_range(page_obj),
        "per_page": per_page,
        "allowed_per_page": ALLOWED_PER_PAGE,
        "query_params": f"&per_page={per_page}",
        "active_nav": "sessions",
    }
    return render(request, "test_sessions/list.html", context)


@login_required
@require_POST
def session_start(request: AuthenticatedRequest) -> HttpResponse:
    form = TestSessionStartForm(request.POST)
    if not form.is_valid():
        messages.error(request, _form_errors(form))
        return redirect("test_sessions:list")
    try:
        session = start_session(
            user=request.user,
            name=form.cleaned_data["name"],
            note=form.cleaned_data["note"],
        )
    except SessionAlreadyOpenError as exc:
        messages.error(request, exc.message)
        if exc.open_session_id is not None:
            return _detail_redirect(exc.open_session_id)
        return redirect("test_sessions:list")
    messages.success(request, f"Started “{session.display_name}”.")
    return _detail_redirect(session.id)


@login_required
@require_safe
def session_detail(request: AuthenticatedRequest, session_id: int) -> HttpResponse:
    try:
        session = get_session_for_user(request.user, session_id)
    except SessionNotFoundError as exc:
        raise Http404 from exc
    per_page = parse_per_page(request)
    page_obj = list_session_cases(
        session, request.user, page=parse_page(request), per_page=per_page
    )
    context: dict[str, object] = {
        "session": session,
        "summary": get_session_summary(session),
        "page_obj": page_obj,
        "elided_page_range": elided_page_range(page_obj),
        "per_page": per_page,
        "allowed_per_page": ALLOWED_PER_PAGE,
        "query_params": f"&per_page={per_page}",
        # Distinct auto_ids: both dialogs render a `note` field on one page.
        "stop_form": TestSessionStopForm(auto_id="stop_%s"),
        "edit_form": TestSessionEditForm(
            auto_id="edit_%s", initial={"name": session.name, "note": session.note}
        ),
        "export_url": reverse("test_sessions:export", args=[session.id]),
        "active_nav": "sessions",
    }
    return render(request, "test_sessions/detail.html", context)


@login_required
@require_POST
def session_stop(request: AuthenticatedRequest, session_id: int) -> HttpResponse:
    form = TestSessionStopForm(request.POST)
    if not form.is_valid():
        messages.error(request, _form_errors(form))
        return _detail_redirect(session_id)
    note: str = form.cleaned_data["note"]
    try:
        session = stop_session(
            user=request.user, session_id=session_id, note=note or None
        )
    except SessionNotFoundError as exc:
        raise Http404 from exc
    except SessionAlreadyClosedError as exc:
        messages.info(request, exc.message)
        return _detail_redirect(session_id)
    messages.success(request, f"Stopped “{session.display_name}”.")
    return _detail_redirect(session.id)


@login_required
@require_POST
def session_edit(request: AuthenticatedRequest, session_id: int) -> HttpResponse:
    form = TestSessionEditForm(request.POST)
    if not form.is_valid():
        messages.error(request, _form_errors(form))
        return _detail_redirect(session_id)
    try:
        edit_session(
            user=request.user,
            session_id=session_id,
            name=form.cleaned_data["name"],
            note=form.cleaned_data["note"],
        )
    except SessionNotFoundError as exc:
        raise Http404 from exc
    messages.success(request, "Saved the session.")
    return _detail_redirect(session_id)


@login_required
@require_POST
def session_delete(request: AuthenticatedRequest, session_id: int) -> HttpResponse:
    try:
        delete_session(user=request.user, session_id=session_id)
    except SessionNotFoundError as exc:
        raise Http404 from exc
    messages.success(request, "Deleted the test session.")
    return redirect("test_sessions:list")


@login_required
@require_safe
def session_export(request: AuthenticatedRequest, session_id: int) -> HttpResponseBase:
    try:
        session = get_session_for_user(request.user, session_id)
    except SessionNotFoundError as exc:
        raise Http404 from exc
    try:
        check_session_export_throttle(request.user)
    except SessionExportThrottledError as exc:
        messages.error(request, exc.message)
        return _detail_redirect(session.id)
    response = StreamingHttpResponse(
        stream_session_csv(session.id), content_type="text/csv; charset=utf-8"
    )
    filename = f"test-session-{session.id}-{timezone.now():%Y%m%d}.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "no-store"
    return response
