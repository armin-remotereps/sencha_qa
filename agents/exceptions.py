from __future__ import annotations

from enum import StrEnum

from agents.types import PixelUIElement


class ElementNotFoundError(Exception):
    pass


class RejectedElementChosenError(ElementNotFoundError):
    """The matcher picked an element that verification had already ruled out."""

    def __init__(self, element: PixelUIElement, description: str) -> None:
        super().__init__(
            f"Matcher chose already rejected element [{element.index}] "
            f"'{element.content}' for: {description}"
        )
        self.element = element


class ActionVerificationError(Exception):
    """Raised when a desktop action could not be confirmed within the attempt budget."""


class InfrastructureError(Exception):
    """The case cannot be executed or judged; never a verdict on the application."""


class AIServiceErrorKind(StrEnum):
    RATE_LIMITED = "rate_limited"
    AUTH = "auth"
    UNAVAILABLE = "unavailable"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    BAD_RESPONSE = "bad_response"


class AIServiceError(InfrastructureError):
    """The LLM provider failed; ``message`` is safe to show and push to TestRail."""

    def __init__(
        self,
        kind: AIServiceErrorKind,
        message: str,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status_code = status_code


class ResumeStateLostError(InfrastructureError):
    """A resumed case could not restore the state its earlier steps left behind."""

    def __init__(self, step_num: int, summary: str) -> None:
        super().__init__(summary)
        self.step_num = step_num
        self.summary = summary


class OrchestratorPlanError(AIServiceError):
    def __init__(self, message: str) -> None:
        super().__init__(AIServiceErrorKind.BAD_RESPONSE, message)


class OrchestratorParseError(AIServiceError):
    def __init__(self, message: str) -> None:
        super().__init__(AIServiceErrorKind.BAD_RESPONSE, message)


class PlanRevisionInvalid(Exception):
    """A proposed plan redesign breaks a rule; ``message`` is written for the model."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
