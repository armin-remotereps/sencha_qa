from __future__ import annotations

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
