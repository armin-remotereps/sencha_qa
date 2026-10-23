from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass


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
            report(f"[FAIL] {step.name}: {e}")
            return False
        report(f"[ok] {step.name}: {detail}")
    return True
