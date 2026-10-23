from collections.abc import Callable, Iterable
from dataclasses import dataclass

from vision_worker.exceptions import EngineError


@dataclass(frozen=True)
class DiagnosticStep:
    name: str
    run: Callable[[], str]


def run_diagnostics(
    steps: Iterable[DiagnosticStep], report: Callable[[str], None]
) -> bool:
    """Run steps in order, reporting each; stop at the first failure."""
    for step in steps:
        try:
            detail = step.run()
        except Exception as e:
            report(f"[FAIL] {step.name}: {_describe_failure(e)}")
            return False
        report(f"[ok] {step.name}: {detail}")
    return True


def _describe_failure(error: Exception) -> str:
    if isinstance(error, EngineError):
        return f"{error} [{error.details()}]"
    return str(error)
