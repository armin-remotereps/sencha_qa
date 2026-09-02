from __future__ import annotations

from django.conf import settings
from django.test import SimpleTestCase

from projects.tasks import execute_test_run_case


class ControllerTimeoutOrderingTests(SimpleTestCase):
    def test_timeouts_are_ordered_so_a_stuck_find_element_call_cannot_stall_the_task(
        self,
    ) -> None:
        """Each layer must time out before the layer wrapping it, otherwise a
        stuck controller call gets killed mid-flight by Celery's hard time
        limit instead of surfacing a clean, catchable error."""
        find_element_timeout = settings.CONTROLLER_FIND_ELEMENT_TIMEOUT
        min_sub_agent_timeout = settings.SUB_AGENT_MIN_TIMEOUT_SECONDS
        default_sub_agent_timeout = settings.SUB_AGENT_TIMEOUT_SECONDS
        max_sub_agent_timeout = settings.SUB_AGENT_MAX_TIMEOUT_SECONDS
        soft_time_limit = execute_test_run_case.soft_time_limit
        time_limit = execute_test_run_case.time_limit
        assert soft_time_limit is not None
        assert time_limit is not None

        self.assertLess(find_element_timeout, min_sub_agent_timeout)
        self.assertLessEqual(min_sub_agent_timeout, default_sub_agent_timeout)
        self.assertLessEqual(default_sub_agent_timeout, max_sub_agent_timeout)
        self.assertLess(max_sub_agent_timeout, soft_time_limit)
        self.assertLess(soft_time_limit, time_limit)
