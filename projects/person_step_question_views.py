"""Ask AI from the web "Your turn" card (Human in the loop).

Validates the form, calls ``person_step_question_services``, maps
``PersonStepQuestionError`` to a flash message and returns to the card's Ask
section. The answer arrives on the case page's socket.
"""

from __future__ import annotations

from django.contrib import messages
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.types import AuthenticatedRequest
from projects.case_help_services import get_run_case_for_help
from projects.decorators import project_membership_required
from projects.exceptions import PersonStepQuestionError
from projects.forms import PersonStepQuestionForm, form_error_text
from projects.models import Project
from projects.person_step_actor import PersonStepActor
from projects.person_step_question_services import ask_person_step_question

_ASK_ANCHOR = "#person-step-ask"
# Tells the case page's question draft to put the refused question back.
_ASK_REFUSED_QUERY = "?ask=refused"


@project_membership_required
@require_POST
def test_run_case_person_ask(
    request: AuthenticatedRequest, project: Project, test_run_id: int, pivot_id: int
) -> HttpResponse:
    pivot = get_run_case_for_help(pivot_id, test_run_id, project)
    if pivot is None:
        raise Http404
    form = PersonStepQuestionForm(request.POST)
    refused = True
    if not form.is_valid():
        messages.error(request, form_error_text(form))
    else:
        try:
            ask_person_step_question(
                pivot=pivot,
                project_id=project.id,
                actor=PersonStepActor.web(request.user),
                person_step_id=form.cleaned_data["person_step_id"],
                question=form.cleaned_data["question"],
            )
            refused = False
        except PersonStepQuestionError as exc:
            messages.error(request, exc.message)
    case_url = reverse(
        "projects:test_run_case_detail",
        kwargs={
            "project_id": project.id,
            "test_run_id": pivot.test_run_id,
            "pivot_id": pivot.id,
        },
    )
    query = _ASK_REFUSED_QUERY if refused else ""
    return HttpResponseRedirect(case_url + query + _ASK_ANCHOR)
