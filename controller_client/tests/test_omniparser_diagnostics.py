from __future__ import annotations

from controller_client.exceptions import OmniParserError
from controller_client.omniparser_diagnostics import (
    DiagnosticStep,
    build_default_steps,
    run_diagnostics,
)


def test_run_diagnostics_reports_every_passing_step() -> None:
    lines: list[str] = []
    steps = (
        DiagnosticStep("imports", lambda: "torch 2.10"),
        DiagnosticStep("weights", lambda: "/weights"),
    )

    assert run_diagnostics(steps, lines.append) is True
    assert lines == ["[ok] imports: torch 2.10", "[ok] weights: /weights"]


def test_run_diagnostics_stops_at_first_failure() -> None:
    lines: list[str] = []
    ran: list[str] = []

    def passing() -> str:
        ran.append("first")
        return "fine"

    def failing() -> str:
        ran.append("second")
        raise RuntimeError("weights missing; run the setup script")

    def never() -> str:
        ran.append("third")
        return "unreachable"

    steps = (
        DiagnosticStep("first", passing),
        DiagnosticStep("second", failing),
        DiagnosticStep("third", never),
    )

    assert run_diagnostics(steps, lines.append) is False
    assert lines == [
        "[ok] first: fine",
        "[FAIL] second: weights missing; run the setup script",
    ]
    assert ran == ["first", "second"]


def test_run_diagnostics_includes_omniparser_error_details() -> None:
    lines: list[str] = []

    def failing() -> str:
        raise OmniParserError(
            "model load blew up",
            phase="model_load",
            device="cpu",
            weights_dir="/w",
            code="OMNIPARSER_NOT_READY",
        )

    run_diagnostics((DiagnosticStep("model", failing),), lines.append)

    assert lines == [
        "[FAIL] model: model load blew up [phase=model_load; device=cpu; weights_dir=/w]"
    ]


def test_default_steps_are_ordered_and_skip_inference_drops_last_two() -> None:
    full = [step.name for step in build_default_steps(skip_inference=False)]
    short = [step.name for step in build_default_steps(skip_inference=True)]

    assert full == [
        "required imports",
        "weights directory",
        "weight files",
        "screenshot",
        "device",
        "model construction",
        "inference",
        "result size",
    ]
    assert short == full[:-2]
