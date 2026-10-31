"""Knowledge base tab: thin views over the knowledge_base services."""

from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.http import FileResponse, Http404, HttpResponse
from django.http.response import HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import content_disposition_header, urlencode
from django.views.decorators.http import require_GET, require_POST

from accounts.types import AuthenticatedRequest
from knowledge_base.exceptions import (
    KnowledgeBaseError,
    KnowledgeFolderNotFoundError,
    KnowledgeItemNotFoundError,
    KnowledgeItemNotReadyError,
    KnowledgePageOutOfRangeError,
    KnowledgeQueryInvalidError,
)
from knowledge_base.formats import SEVEN_ZIP_DISABLED_MESSAGE, accept_attribute
from knowledge_base.forms import (
    KnowledgeItemEditForm,
    KnowledgeSearchForm,
    KnowledgeUploadForm,
)
from knowledge_base.models import KNOWLEDGE_FOLDER_MAX_LENGTH, KnowledgeKind
from knowledge_base.services.browse_knowledge_archive import (
    browse_knowledge_archive,
)
from knowledge_base.services.delete_knowledge_item import delete_knowledge_item
from knowledge_base.services.download_knowledge_item import open_knowledge_download
from knowledge_base.services.edit_knowledge_item import edit_knowledge_item
from knowledge_base.services.list_knowledge_items import (
    get_knowledge_item_detail,
    knowledge_usage_bytes,
    list_knowledge_entries,
)
from knowledge_base.services.list_knowledge_tags import list_knowledge_tags
from knowledge_base.services.read_knowledge_item import read_knowledge_item
from knowledge_base.services.retry_knowledge_item import retry_knowledge_item
from knowledge_base.services.search_knowledge import search_knowledge
from knowledge_base.services.upload_knowledge_files import upload_knowledge_files
from knowledge_base.tags import normalise_folder_query
from knowledge_base.types import (
    KnowledgeArchiveBrowse,
    KnowledgeHit,
    KnowledgeItemDetail,
    KnowledgeListEntry,
    KnowledgePage,
    KnowledgeTagFacet,
)
from projects.decorators import project_membership_required
from projects.models import Project

SEARCH_RESULT_LIMIT = 20
# Rows on the tab; older items stay searchable (and listed to agents).
LIST_ROW_LIMIT = 500
# nginx's client_max_body_size: the whole upload request, stated on the page.
UPLOAD_REQUEST_MAX_BYTES = 200 * 1024 * 1024


def _ws_path(project_id: int) -> str:
    return f"/ws/projects/{project_id}/knowledge-base/"


def _positive_int(raw: str | None) -> int | None:
    """A positive integer query value, or None when absent or malformed."""
    if raw is None or not raw.isdigit():
        return None
    value = int(raw)
    return value if value > 0 else None


def _get_detail(project_id: int, item_id: int) -> KnowledgeItemDetail:
    try:
        return get_knowledge_item_detail(project_id, item_id)
    except KnowledgeItemNotFoundError as exc:
        raise Http404(exc.message) from exc


def _item_redirect(project_id: int, item_id: int) -> HttpResponse:
    return redirect("knowledge_base:item", project_id=project_id, item_id=item_id)


def _list_url(project_id: int, archive_id: int | None = None) -> str:
    url = reverse("knowledge_base:list", kwargs={"project_id": project_id})
    return f"{url}?archive={archive_id}" if archive_id is not None else url


def _browse(
    project_id: int, archive_param: str, folder_param: str
) -> KnowledgeArchiveBrowse:
    """``?archive=<id>&folder=<path>``; 404 for anything that is not a folder."""
    archive_id = _positive_int(archive_param)
    if archive_id is None or len(folder_param) > KNOWLEDGE_FOLDER_MAX_LENGTH:
        raise Http404("No such archive folder.")
    try:
        browse = browse_knowledge_archive(
            project_id, archive_id, folder=normalise_folder_query(folder_param)
        )
    except (KnowledgeItemNotFoundError, KnowledgeFolderNotFoundError) as exc:
        raise Http404(exc.message) from exc
    if browse.archive.entry.summary.kind != KnowledgeKind.ARCHIVE:
        raise Http404("No such archive.")
    return browse


def _tag_listed(facet: KnowledgeTagFacet, tag: str) -> bool:
    """Whether the tag picker already offers ``tag`` as an option."""
    return any(count.tag == tag for count in (*facet.tags, *facet.folders))


@project_membership_required
@require_GET
def knowledge_list(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    archive_param = request.GET.get("archive")
    browse: KnowledgeArchiveBrowse | None = None
    if archive_param is not None:
        browse = _browse(project.id, archive_param, request.GET.get("folder", ""))

    bound = "q" in request.GET or "tag" in request.GET
    search_form = KnowledgeSearchForm(request.GET if bound else None)
    query = ""
    active_tag = ""
    if search_form.is_bound and search_form.is_valid():
        query = search_form.cleaned_data["q"]
        active_tag = search_form.cleaned_data["tag"]

    items: list[KnowledgeListEntry] = []
    items_capped = False
    if browse is None:
        items = list_knowledge_entries(
            project.id, tag=active_tag or None, limit=LIST_ROW_LIMIT + 1
        )
        items_capped = len(items) > LIST_ROW_LIMIT
        items = items[:LIST_ROW_LIMIT]

    hits: list[KnowledgeHit] | None = None
    if query:
        try:
            hits = search_knowledge(
                project.id,
                query,
                limit=SEARCH_RESULT_LIMIT,
                exact=search_form.cleaned_data["exact"],
                tag=active_tag or None,
            )
        except KnowledgeQueryInvalidError as exc:
            search_form.add_error("q", exc.message)

    tag_facet = list_knowledge_tags(project.id)
    accept_7z = settings.KNOWLEDGE_BASE_ACCEPT_7Z
    return render(
        request,
        "knowledge_base/list.html",
        {
            "project": project,
            "active_tab": "knowledge_base",
            "items": items,
            "items_capped": items_capped,
            "list_row_limit": LIST_ROW_LIMIT,
            "browse": browse,
            "hits": hits,
            "search_form": search_form,
            "tag_facet": tag_facet,
            "active_tag": active_tag,
            "active_tag_listed": _tag_listed(tag_facet, active_tag),
            "clear_tag_url": _clear_tag_url(project.id, query, search_form),
            "upload_form": KnowledgeUploadForm(),
            "usage_bytes": knowledge_usage_bytes(project.id),
            "quota_bytes": settings.KNOWLEDGE_BASE_MAX_PROJECT_BYTES,
            "limits": {
                "max_file_bytes": settings.KNOWLEDGE_BASE_MAX_FILE_BYTES,
                "max_files": settings.KNOWLEDGE_BASE_MAX_FILES_PER_UPLOAD,
                "max_request_bytes": UPLOAD_REQUEST_MAX_BYTES,
                "max_archive_bytes": settings.KNOWLEDGE_BASE_MAX_ARCHIVE_BYTES,
                "max_archive_files": settings.KNOWLEDGE_BASE_ARCHIVE_MAX_MEMBERS,
                "accept": accept_attribute(accept_7z=accept_7z),
            },
            "accept_7z": accept_7z,
            "seven_zip_disabled_message": SEVEN_ZIP_DISABLED_MESSAGE,
            "ws_path": _ws_path(project.id),
        },
    )


def _clear_tag_url(project_id: int, query: str, form: KnowledgeSearchForm) -> str:
    """The list URL keeping the search but dropping the tag filter."""
    url = reverse("knowledge_base:list", kwargs={"project_id": project_id})
    if not query:
        return url
    params = {"q": query}
    if form.cleaned_data.get("exact"):
        params["exact"] = "on"
    return f"{url}?{urlencode(params)}"


@project_membership_required
@require_POST
def knowledge_upload(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    form = KnowledgeUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, str(error))
        return redirect(_list_url(project.id))
    try:
        outcome = upload_knowledge_files(
            project_id=project.id,
            user=request.user,
            files=form.cleaned_files(),
            description=form.cleaned_data["description"],
            tags=form.cleaned_data["tags"],
        )
    except KnowledgeBaseError as exc:
        messages.error(request, exc.message)
        return redirect(_list_url(project.id))
    if outcome.refused:
        refused = "; ".join(f"{file.name}: {file.reason}" for file in outcome.refused)
        count = len(outcome.refused)
        messages.error(
            request, f"{count} file{'s' if count != 1 else ''} not added. {refused}"
        )
    created = len(outcome.created_ids)
    if created:
        messages.success(
            request,
            f"Added {created} file{'s' if created != 1 else ''}. "
            "They are being read in the background.",
        )
    return redirect(_list_url(project.id))


@project_membership_required
@require_GET
def knowledge_item(
    request: AuthenticatedRequest, project: Project, item_id: int
) -> HttpResponse:
    detail = _get_detail(project.id, item_id)
    summary = detail.entry.summary
    page: KnowledgePage | None = None
    browse: KnowledgeArchiveBrowse | None = None
    if summary.kind == KnowledgeKind.ARCHIVE:
        try:
            browse = browse_knowledge_archive(project.id, item_id, folder="")
        except (KnowledgeItemNotFoundError, KnowledgeFolderNotFoundError) as exc:
            raise Http404(exc.message) from exc
    else:
        requested = _positive_int(request.GET.get("page")) or 1
        try:
            try:
                page = read_knowledge_item(project.id, item_id, page=requested)
            except KnowledgePageOutOfRangeError:
                page = read_knowledge_item(project.id, item_id, page=1)
        except (KnowledgeItemNotReadyError, KnowledgePageOutOfRangeError):
            page = None
        except KnowledgeItemNotFoundError as exc:
            raise Http404(exc.message) from exc

    return render(
        request,
        "knowledge_base/item.html",
        {
            "project": project,
            "active_tab": "knowledge_base",
            "item": detail,
            "page": page,
            "browse": browse,
            "edit_form": KnowledgeItemEditForm(
                initial={
                    "name": summary.name,
                    "description": summary.description,
                    "tags": ", ".join(summary.tags),
                }
            ),
            "ws_path": _ws_path(project.id),
        },
    )


@project_membership_required
@require_POST
def knowledge_item_edit(
    request: AuthenticatedRequest, project: Project, item_id: int
) -> HttpResponse:
    form = KnowledgeItemEditForm(request.POST)
    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, str(error))
        return _item_redirect(project.id, item_id)
    try:
        edit_knowledge_item(
            project_id=project.id,
            item_id=item_id,
            name=form.cleaned_data["name"],
            description=form.cleaned_data["description"],
            tags=form.cleaned_data["tags"],
        )
    except KnowledgeItemNotFoundError as exc:
        raise Http404(exc.message) from exc
    except KnowledgeBaseError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, "Saved.")
    return _item_redirect(project.id, item_id)


@project_membership_required
@require_POST
def knowledge_item_retry(
    request: AuthenticatedRequest, project: Project, item_id: int
) -> HttpResponse:
    try:
        retry_knowledge_item(project_id=project.id, item_id=item_id)
    except KnowledgeItemNotFoundError as exc:
        raise Http404(exc.message) from exc
    except KnowledgeBaseError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, "Reading the file again.")
    return _item_redirect(project.id, item_id)


@project_membership_required
@require_POST
def knowledge_item_delete(
    request: AuthenticatedRequest, project: Project, item_id: int
) -> HttpResponse:
    try:
        parent_id = delete_knowledge_item(project_id=project.id, item_id=item_id)
    except KnowledgeItemNotFoundError as exc:
        raise Http404(exc.message) from exc
    messages.success(request, "Deleted.")
    return redirect(_list_url(project.id, parent_id))


@project_membership_required
@require_GET
def knowledge_item_download(
    request: AuthenticatedRequest, project: Project, item_id: int
) -> HttpResponseBase:
    try:
        download = open_knowledge_download(project_id=project.id, item_id=item_id)
    except KnowledgeItemNotFoundError as exc:
        raise Http404(exc.message) from exc
    response = FileResponse(
        download.file,
        as_attachment=True,
        filename=download.file_name,
        content_type="application/octet-stream",
    )
    disposition = content_disposition_header(
        as_attachment=True, filename=download.file_name
    )
    # Never None with as_attachment=True; narrowed for the type checker.
    if disposition is not None:
        response["Content-Disposition"] = disposition
    response["X-Content-Type-Options"] = "nosniff"
    return response
