"""The ``/runners/`` pages: a user's own Test Runners (plan:
docs/plan/feat-user-test-runners.md › Pages and forms).

Thin views: validate the form, call one ``runners.services`` use case, map
``RunnerError`` to a message. Owner checks live in the services
(``runner_access``); another user's runner answers 404. Every write is POST.

A new token (add, new token) is shown once: it is put in the session under
``_ISSUED_TOKEN_KEY`` and popped by the next detail page render, which is
``never_cache``. It never goes into a URL, a message or a log.
"""

from __future__ import annotations

from typing import TypedDict

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from accounts.types import AuthenticatedRequest
from projects.exceptions import RunnerUpdateRefusedError
from projects.project_access import has_superuser_project_control
from projects.runner_update_request import request_runner_update
from runners.exceptions import (
    RunnerError,
    RunnerNotFoundError,
    RunnerPermissionError,
)
from runners.forms import (
    RunnerConfirmForm,
    RunnerCreateForm,
    RunnerDisconnectForm,
    RunnerRenameForm,
    RunnerUpdateForm,
)
from runners.services.archive_runner import archive_runner
from runners.services.create_runner import create_runner
from runners.services.disconnect_runner import disconnect_runner
from runners.services.reissue_token import reissue_runner_token
from runners.services.release_runner import release_runner
from runners.services.rename_runner import rename_runner
from runners.services.runner_detail import get_owned_runner, get_runner_detail
from runners.services.runner_list import list_all_runners, list_owner_runners
from runners.services.runner_zip import build_runner_download
from runners.types import RunnerId

_ISSUED_TOKEN_KEY = "runners_issued_token"
_STALE_FORM_MESSAGE = "Reload the page and try again."


class _IssuedToken(TypedDict):
    runner_id: int
    token: str


def _flash_token(request: AuthenticatedRequest, runner_id: int, token: str) -> None:
    """Keep the new token for the next detail render only.

    Decision (plan review, Low): the add and new-token POSTs keep
    Post/Redirect/Get rather than rendering the token in their own
    ``no-store`` response, so a reload never re-posts (a second new token)
    and the token never sits in a POST response the browser may offer to
    resubmit. It lives in the session until ``runner_detail`` (``never_cache``)
    pops it on its first render; it is never put in a URL, a message or a log.
    """
    issued: _IssuedToken = {"runner_id": runner_id, "token": token}
    request.session[_ISSUED_TOKEN_KEY] = issued


def _pop_token(request: AuthenticatedRequest, runner_id: int) -> str:
    """The token issued for this runner, once; "" otherwise."""
    issued = request.session.get(_ISSUED_TOKEN_KEY)
    if not isinstance(issued, dict) or issued.get("runner_id") != runner_id:
        return ""
    del request.session[_ISSUED_TOKEN_KEY]
    token = issued.get("token")
    return token if isinstance(token, str) else ""


def _detail(runner_id: int) -> HttpResponse:
    return redirect("runners:detail", runner_id=runner_id)


def _render_list(
    request: AuthenticatedRequest, add_form: RunnerCreateForm, status: int = 200
) -> HttpResponse:
    rows = list_owner_runners(request.user)
    max_runners: int = settings.RUNNER_MAX_PER_USER
    return render(
        request,
        "runners/list.html",
        {
            "active_nav": "runners",
            "rows": rows,
            "add_form": add_form,
            "max_runners": max_runners,
            "at_limit": len(rows) >= max_runners,
            "is_superuser_viewer": has_superuser_project_control(request.user),
        },
        status=status,
    )


@login_required
@require_GET
def runner_list(request: AuthenticatedRequest) -> HttpResponse:
    return _render_list(request, RunnerCreateForm())


@login_required
@require_POST
def runner_add(request: AuthenticatedRequest) -> HttpResponse:
    form = RunnerCreateForm(request.POST)
    if not form.is_valid():
        return _render_list(request, form, status=400)
    try:
        created = create_runner(owner=request.user, name=form.cleaned_data["name"])
    except RunnerError as exc:
        form.add_error("name", exc.message)
        return _render_list(request, form, status=400)
    _flash_token(request, created.runner.pk, created.token)
    messages.success(request, "Test Runner added. Download it and set it up below.")
    return _detail(created.runner.pk)


@login_required
@require_GET
@never_cache
def runner_detail(request: AuthenticatedRequest, runner_id: int) -> HttpResponse:
    try:
        detail = get_runner_detail(actor=request.user, runner_id=RunnerId(runner_id))
    except (RunnerNotFoundError, RunnerPermissionError):
        raise Http404("Test Runner not found.") from None
    return render(
        request,
        "runners/detail.html",
        {
            "active_nav": "runners",
            "detail": detail,
            "runner": detail.runner,
            "issued_token": _pop_token(request, detail.runner.pk),
            "rename_form": RunnerRenameForm(initial={"name": detail.runner.name}),
        },
    )


@login_required
@require_POST
def runner_rename(request: AuthenticatedRequest, runner_id: int) -> HttpResponse:
    form = RunnerRenameForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Enter a name of at most 64 characters.")
        return _detail(runner_id)
    try:
        rename_runner(
            actor=request.user,
            runner_id=RunnerId(runner_id),
            name=form.cleaned_data["name"],
        )
    except (RunnerNotFoundError, RunnerPermissionError):
        raise Http404("Test Runner not found.") from None
    except RunnerError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, "Test Runner renamed.")
    return _detail(runner_id)


@login_required
@require_POST
def runner_download(request: AuthenticatedRequest, runner_id: int) -> HttpResponse:
    try:
        download = build_runner_download(
            actor=request.user, runner_id=RunnerId(runner_id)
        )
    except RunnerError:
        raise Http404("Test Runner not found.") from None
    response = HttpResponse(download.archive, content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{download.filename}"'
    response["Cache-Control"] = "no-store"
    return response


@login_required
@require_POST
def runner_token(request: AuthenticatedRequest, runner_id: int) -> HttpResponse:
    form = RunnerConfirmForm(request.POST)
    if not form.is_valid():
        messages.error(request, _STALE_FORM_MESSAGE)
        return _detail(runner_id)
    try:
        token = reissue_runner_token(actor=request.user, runner_id=RunnerId(runner_id))
    except RunnerError:
        raise Http404("Test Runner not found.") from None
    _flash_token(request, runner_id, token)
    messages.success(
        request,
        "New token issued. The old one no longer works: download the Test Runner "
        "again, or put the new token in its .env.",
    )
    return _detail(runner_id)


@login_required
@require_POST
def runner_update(request: AuthenticatedRequest, runner_id: int) -> HttpResponse:
    form = RunnerUpdateForm(request.POST)
    if not form.is_valid():
        messages.error(request, _STALE_FORM_MESSAGE)
        return _detail(runner_id)
    try:
        runner = get_owned_runner(actor=request.user, runner_id=RunnerId(runner_id))
    except (RunnerNotFoundError, RunnerPermissionError):
        raise Http404("Test Runner not found.") from None
    try:
        update = request_runner_update(
            runner_id=runner.runner_id,
            user=request.user,
            expected_version=form.cleaned_data["expected_version"],
        )
    except RunnerUpdateRefusedError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(
            request,
            f"Update to Test Runner {update.to_version} sent. Follow its progress "
            "on this page.",
        )
    return _detail(runner_id)


@login_required
@require_POST
def runner_release(request: AuthenticatedRequest, runner_id: int) -> HttpResponse:
    form = RunnerConfirmForm(request.POST)
    if not form.is_valid():
        messages.error(request, _STALE_FORM_MESSAGE)
        return _detail(runner_id)
    try:
        released = release_runner(actor=request.user, runner_id=RunnerId(runner_id))
    except (RunnerNotFoundError, RunnerPermissionError):
        raise Http404("Test Runner not found.") from None
    except RunnerError as exc:
        messages.error(request, exc.message)
    else:
        if released:
            messages.success(request, "Test Runner released from its runs.")
        else:
            messages.info(request, "No active run holds this Test Runner.")
    return _detail(runner_id)


@login_required
@require_POST
def runner_disconnect(request: AuthenticatedRequest, runner_id: int) -> HttpResponse:
    form = RunnerDisconnectForm(request.POST)
    back_to_all = (
        form.is_valid()
        and form.cleaned_data["return_to"] == RunnerDisconnectForm.RETURN_ALL
    )
    try:
        disconnected = disconnect_runner(
            actor=request.user, runner_id=RunnerId(runner_id)
        )
    except (RunnerNotFoundError, RunnerPermissionError):
        raise Http404("Test Runner not found.") from None
    if disconnected:
        messages.success(request, "Test Runner disconnected.")
    else:
        messages.info(request, "The Test Runner is not connected.")
    if back_to_all:
        return redirect("runners:all")
    return _detail(runner_id)


@login_required
@require_POST
def runner_archive(request: AuthenticatedRequest, runner_id: int) -> HttpResponse:
    form = RunnerConfirmForm(request.POST)
    if not form.is_valid():
        messages.error(request, _STALE_FORM_MESSAGE)
        return _detail(runner_id)
    try:
        runner = archive_runner(actor=request.user, runner_id=RunnerId(runner_id))
    except (RunnerNotFoundError, RunnerPermissionError):
        raise Http404("Test Runner not found.") from None
    except RunnerError as exc:
        messages.error(request, exc.message)
        return _detail(runner_id)
    messages.success(request, f"Test Runner “{runner.name}” removed.")
    return redirect("runners:list")


@login_required
@require_GET
def runner_all(request: AuthenticatedRequest) -> HttpResponse:
    if not has_superuser_project_control(request.user):
        raise PermissionDenied
    return render(
        request,
        "runners/all.html",
        {
            "active_nav": "all_runners",
            "rows": list_all_runners(request.user),
        },
    )
