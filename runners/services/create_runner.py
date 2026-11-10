"""Create a Test Runner for its owner and mint its token (shown once)."""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction

from accounts.models import CustomUser
from runners.exceptions import RunnerLimitReachedError, RunnerNameTakenError
from runners.models import TestRunner
from runners.names import normalize_runner_name
from runners.tokens import mint_runner_token


@dataclass(frozen=True)
class CreatedRunner:
    runner: TestRunner
    # Return it to the owner once (session flash), never in a URL or a log.
    token: str


def create_runner(*, owner: CustomUser, name: str) -> CreatedRunner:
    """A new runner of ``owner``.

    Raises ``RunnerNameInvalidError``, ``RunnerNameTakenError`` (case-
    insensitive among the owner's non-archived runners) or
    ``RunnerLimitReachedError`` (``RUNNER_MAX_PER_USER``). The owner row is
    locked so two concurrent creates cannot pass the limit together.
    """
    clean_name = normalize_runner_name(name)
    limit: int = settings.RUNNER_MAX_PER_USER
    token = mint_runner_token()
    try:
        with transaction.atomic():
            CustomUser.objects.select_for_update().filter(pk=owner.pk).first()
            active = TestRunner.objects.filter(
                owner_id=owner.pk, archived_at__isnull=True
            )
            if active.count() >= limit:
                raise RunnerLimitReachedError(limit)
            if active.filter(name__iexact=clean_name).exists():
                raise RunnerNameTakenError()
            runner = TestRunner.objects.create(
                owner=owner,
                name=clean_name,
                token_hash=token.token_hash,
                token_encrypted=token.token_encrypted,
            )
    except IntegrityError as exc:
        # The name constraint (a concurrent create of the same name).
        raise RunnerNameTakenError() from exc
    return CreatedRunner(runner=runner, token=token.plaintext)
