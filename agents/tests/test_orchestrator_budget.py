from __future__ import annotations

import json
from typing import Any, Literal
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from agents.services import orchestrator
from agents.services.orchestrator_prompts import (
    build_evaluate_prompt,
    build_plan_system_prompt,
)
from agents.services.sub_task_budget import TimeoutBounds
from agents.types import (
    AgentConfig,
    ChatMessage,
    LLMConfig,
    LLMResponse,
    SubTask,
    SubTaskResult,
)

_LLM = LLMConfig(
    model="test-model",
    api_key="key",
    endpoint_url="http://llm.test",
    temperature=0.0,
    max_tokens=100,
)

_BOUNDS = TimeoutBounds(default=180, minimum=60, maximum=900)

_ORCHESTRATOR_SETTINGS = {
    "ORCHESTRATOR_MAX_SUBTASKS": 30,
    "ORCHESTRATOR_MAX_RECOVERY_ATTEMPTS": 1,
}


def _json_response(payload: dict[str, Any]) -> LLMResponse:
    return _text_response(json.dumps(payload))


def _text_response(text: str) -> LLMResponse:
    return LLMResponse(
        message=ChatMessage(role="assistant", content=text),
        finish_reason="stop",
        usage_prompt_tokens=1,
        usage_completion_tokens=1,
    )


def _sub_task(description: str, timeout_seconds: int) -> SubTask:
    return SubTask(
        description=description,
        expected_result="done",
        timeout_seconds=timeout_seconds,
    )


@override_settings(**_ORCHESTRATOR_SETTINGS)
class PlanSubTasksBudgetTests(SimpleTestCase):
    def _plan(
        self,
        sub_tasks: list[dict[str, Any]],
        logs: list[str] | None = None,
    ) -> tuple[SubTask, ...]:
        with patch.object(
            orchestrator,
            "send_chat_completion",
            return_value=_json_response({"sub_tasks": sub_tasks}),
        ):
            on_log = logs.append if logs is not None else None
            return orchestrator._plan_sub_tasks(
                _LLM, "Test Case: x", bounds=_BOUNDS, on_log=on_log
            )

    def test_planner_timeout_is_carried_onto_sub_task(self) -> None:
        (sub_task,) = self._plan(
            [
                {
                    "description": "Download",
                    "expected_result": "ok",
                    "timeout_seconds": 600,
                }
            ]
        )
        self.assertEqual(sub_task.timeout_seconds, 600)

    def test_missing_timeout_uses_default(self) -> None:
        (sub_task,) = self._plan([{"description": "Click", "expected_result": "ok"}])
        self.assertEqual(sub_task.timeout_seconds, 180)

    def test_oversized_timeout_is_clamped(self) -> None:
        (sub_task,) = self._plan(
            [
                {
                    "description": "Build",
                    "expected_result": "ok",
                    "timeout_seconds": 99999,
                }
            ]
        )
        self.assertEqual(sub_task.timeout_seconds, 900)

    def test_plan_log_line_shows_budget(self) -> None:
        logs: list[str] = []
        self._plan(
            [
                {
                    "description": "Download",
                    "expected_result": "ok",
                    "timeout_seconds": 600,
                }
            ],
            logs,
        )
        self.assertTrue(any("600s" in line and "Download" in line for line in logs))


@override_settings(**_ORCHESTRATOR_SETTINGS)
class ExecuteSubTasksBudgetTests(SimpleTestCase):
    def test_each_sub_task_runs_with_its_own_timeout(self) -> None:
        seen_timeouts: list[int] = []

        def fake_run_sub_agent(
            sub_task: SubTask, *args: Any, config: AgentConfig, **kwargs: Any
        ) -> SubTaskResult:
            seen_timeouts.append(config.timeout_seconds)
            return SubTaskResult(status="pass", summary="ok", iterations=1)

        with (
            patch.object(orchestrator, "run_sub_agent", side_effect=fake_run_sub_agent),
            patch.object(
                orchestrator,
                "send_chat_completion",
                return_value=_text_response("All good"),
            ),
        ):
            orchestrator._execute_sub_tasks(
                orchestrator_llm=_LLM,
                sub_tasks=(_sub_task("open", 90), _sub_task("download", 600)),
                project_id=1,
                sub_agent_config=AgentConfig(llm=_LLM, timeout_seconds=180),
                bounds=_BOUNDS,
            )

        self.assertEqual(seen_timeouts, [90, 600])

    def test_recovery_task_runs_with_planner_timeout(self) -> None:
        seen: list[tuple[str, int]] = []

        def fake_run_sub_agent(
            sub_task: SubTask, *args: Any, config: AgentConfig, **kwargs: Any
        ) -> SubTaskResult:
            seen.append((sub_task.description, config.timeout_seconds))
            first_download_attempt = seen.count(("download", 600)) == 1
            is_failing = sub_task.description == "download" and first_download_attempt
            status: Literal["pass", "fail"] = "fail" if is_failing else "pass"
            return SubTaskResult(status=status, summary="s", iterations=1)

        decision = _json_response(
            {
                "decision": "recover",
                "reason": "slow",
                "recovery_task": {
                    "description": "keep waiting",
                    "expected_result": "installed",
                    "timeout_seconds": 800,
                },
            }
        )
        with (
            patch.object(orchestrator, "run_sub_agent", side_effect=fake_run_sub_agent),
            patch.object(
                orchestrator,
                "send_chat_completion",
                side_effect=[decision, _text_response("done")],
            ),
        ):
            orchestrator._execute_sub_tasks(
                orchestrator_llm=_LLM,
                sub_tasks=(_sub_task("download", 600),),
                project_id=1,
                sub_agent_config=AgentConfig(llm=_LLM, timeout_seconds=180),
                bounds=_BOUNDS,
            )

        self.assertEqual(
            seen, [("download", 600), ("keep waiting", 800), ("download", 600)]
        )


class PromptBudgetTests(SimpleTestCase):
    def test_plan_prompt_requests_timeout_within_bounds(self) -> None:
        prompt = build_plan_system_prompt(
            min_timeout_seconds=60, max_timeout_seconds=900
        )
        self.assertIn('"timeout_seconds"', prompt)
        self.assertIn("60", prompt)
        self.assertIn("900", prompt)

    def test_evaluate_prompt_requests_recovery_timeout(self) -> None:
        prompt = build_evaluate_prompt(
            _sub_task("download", 600),
            SubTaskResult(status="fail", summary="timed out", iterations=2),
            "state",
            1,
            min_timeout_seconds=60,
            max_timeout_seconds=900,
        )
        self.assertIn('"timeout_seconds"', prompt)
        self.assertIn("600", prompt)
