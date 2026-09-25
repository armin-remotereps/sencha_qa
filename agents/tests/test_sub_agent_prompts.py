from __future__ import annotations

from django.test import SimpleTestCase

from agents.services.sub_agent_prompts import build_sub_agent_system_prompt


class SubAgentPromptBudgetTests(SimpleTestCase):
    def test_prompt_states_time_budget(self) -> None:
        prompt = build_sub_agent_system_prompt(
            "Download PyCharm", "Installed", "", timeout_seconds=600
        )
        self.assertIn("TIME BUDGET", prompt)
        self.assertIn("600 seconds", prompt)


class SubAgentPromptWebToolTests(SimpleTestCase):
    def test_prompt_requires_browser_tools_inside_web_pages(self) -> None:
        prompt = build_sub_agent_system_prompt(
            "Log in", "Dashboard shows", "", timeout_seconds=600
        )
        self.assertIn("MUST be done with browser_* tools", prompt)
        self.assertIn("browser_take_screenshot (not take_screenshot)", prompt)
        self.assertNotIn("DESKTOP FALLBACK", prompt)
