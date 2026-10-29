"""One Ask AI question as the case card, its socket and the Test Runner show it.

A leaf (models, the actor label and the wire types only), so both
``person_step_question_services`` and ``person_step_question_push`` build
their questions here without importing each other.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from controller_client.protocol import (
    PersonStepQuestionPayload,
    PersonStepQuestionWireStatus,
)
from projects.models import PersonStepQuestion, PersonStepQuestionStatus
from projects.person_step_actor import PersonStepActor


@dataclass(frozen=True)
class PersonStepQuestionView:
    """One question of a person step and its answer, as both surfaces show it."""

    question_id: int
    # The step round it was asked in.
    round: int
    status: PersonStepQuestionStatus
    question: str
    # Blank while pending; ``QUESTION_FAILED_ANSWER`` when failed.
    answer: str
    # Server-built label of the asker (``PersonStepActor.label()``).
    asked_by: str
    created_at: datetime

    def to_payload(self) -> PersonStepQuestionPayload:
        """The ``person_step_state`` form of this question."""
        return PersonStepQuestionPayload(
            question_id=self.question_id,
            round=self.round,
            status=PersonStepQuestionWireStatus(self.status.value),
            question=self.question,
            answer=self.answer,
            asked_by=self.asked_by,
        )

    def to_socket_dict(self) -> dict[str, object]:
        """The case page's ``person_step_questions`` event form of this question."""
        return {
            "question_id": self.question_id,
            "round": self.round,
            "status": self.status.value,
            "question": self.question,
            "answer": self.answer,
            "asked_by": self.asked_by,
        }


def build_question_view(row: PersonStepQuestion) -> PersonStepQuestionView:
    """The view of ``row``; ``asked_by`` must be loaded (``select_related``)."""
    return PersonStepQuestionView(
        question_id=row.pk,
        round=row.round,
        status=PersonStepQuestionStatus(row.status),
        question=row.question,
        answer=row.answer,
        asked_by=PersonStepActor.from_row(row.asked_by, row.asked_via).label(),
        created_at=row.created_at,
    )
