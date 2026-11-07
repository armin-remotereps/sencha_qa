"""A person acting on a run case, and how to name them.

Used for person-step answers (Human in the loop), pauses, help resolutions
and help-chat sends. A person acts on the web (a signed-in member) or at the
Test Runner. A
runner answer names a member only when the runner's ``person_email`` matched
an active member of its project at handshake; otherwise it is anonymous. The
label never includes runner-reported text such as the hostname: it reaches
the agent's prompt, the result text and TestRail.
"""

from __future__ import annotations

from dataclasses import dataclass

from accounts.models import CustomUser
from projects.case_result_text import person_label
from projects.models import PersonStepChannel

RUNNER_ANONYMOUS_LABEL = "a person at the Test Runner"
# The neutral tester of a Manual run case performed through the MCP gateway:
# no member, no channel, and nothing naming how the case was performed.
TESTER_LABEL = "a tester"
_RUNNER_SUFFIX = " via Test Runner"


@dataclass(frozen=True)
class PersonStepActor:
    # None only for an anonymous Test Runner answer.
    user: CustomUser | None
    channel: PersonStepChannel

    @classmethod
    def web(cls, user: CustomUser) -> PersonStepActor:
        return cls(user=user, channel=PersonStepChannel.WEB)

    @classmethod
    def runner(cls, user: CustomUser | None) -> PersonStepActor:
        """``user``: the active member the runner's email named, else None."""
        return cls(user=user, channel=PersonStepChannel.RUNNER)

    @classmethod
    def tester(cls) -> PersonStepActor:
        """The neutral tester (no member, no channel): "a tester"."""
        return cls(user=None, channel=PersonStepChannel.NONE)

    @classmethod
    def from_row(cls, user: CustomUser | None, via: str) -> PersonStepActor:
        """Rebuild from a step's ``reported_by``/``reported_via`` (or ``closed_*``).

        A blank ``via`` is a row from before channels existed: the web.
        """
        if via == PersonStepChannel.RUNNER:
            return cls.runner(user)
        return cls(user=user, channel=PersonStepChannel.WEB)

    @property
    def via(self) -> str:
        """The value for ``reported_via`` / ``closed_via``."""
        return str(self.channel.value)

    def label(self) -> str:
        """How texts name the person.

        Web: ``person_label(user)``. Runner with a member: "<member> via Test
        Runner". Anonymous runner: "a person at the Test Runner". Neutral
        tester (no channel): "a tester".
        """
        if self.channel == PersonStepChannel.NONE:
            return TESTER_LABEL
        if self.channel == PersonStepChannel.RUNNER:
            if self.user is None:
                return RUNNER_ANONYMOUS_LABEL
            return person_label(self.user) + _RUNNER_SUFFIX
        return person_label(self.user)
