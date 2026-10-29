from __future__ import annotations


class UserManagementError(Exception):
    """Base for user-management domain errors; ``message`` is user-facing."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UserEmailTakenError(UserManagementError):
    """An account with that email (any letter case) already exists."""

    def __init__(self) -> None:
        super().__init__("A user with that email already exists.")


class SuperuserRequiredError(UserManagementError):
    """Only a superuser may manage users."""

    def __init__(self) -> None:
        super().__init__("Only a superuser can manage users.")
