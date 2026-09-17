from __future__ import annotations

import json
from typing import Any

import httpx
from django.test import SimpleTestCase

from projects.testrail_client import (
    TestRailCase,
    TestRailClient,
    TestRailError,
    TestRailProject,
    TestRailResult,
    TestRailResultInput,
    TestRailRun,
    TestRailSuite,
)

URL = "https://sencha.testrail.com"


def _json(
    status: int, payload: Any, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers or {})


def _paged(
    key: str, items: list[dict[str, Any]], next_link: str | None
) -> dict[str, Any]:
    return {
        "offset": 0,
        "limit": 250,
        "size": len(items),
        "_links": {"next": next_link, "prev": None},
        key: items,
    }


class RecordingTransport(httpx.MockTransport):
    def __init__(self, routes: dict[str, list[httpx.Response]]) -> None:
        self.requests: list[httpx.Request] = []
        self._routes = routes
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = str(request.url).split("/index.php?/api/v2/", 1)[1]
        queue = self._routes[path]
        return queue.pop(0) if len(queue) > 1 else queue[0]


def _client(
    transport: httpx.BaseTransport, sleeps: list[float] | None = None
) -> TestRailClient:
    return TestRailClient(
        url=URL,
        email="qa@example.com",
        api_key="key",
        transport=transport,
        sleep=(sleeps.append if sleeps is not None else lambda _s: None),
    )


class AuthAndUrlTests(SimpleTestCase):
    def test_basic_auth_and_url_shape(self) -> None:
        transport = RecordingTransport(
            {"get_projects": [_json(200, _paged("projects", [], None))]}
        )
        with _client(transport) as client:
            client.get_projects()
        request = transport.requests[0]
        self.assertEqual(str(request.url), f"{URL}/index.php?/api/v2/get_projects")
        self.assertTrue(request.headers["authorization"].startswith("Basic "))
        self.assertEqual(request.headers["content-type"], "application/json")

    def test_trailing_slash_in_url_is_stripped(self) -> None:
        transport = RecordingTransport(
            {"get_projects": [_json(200, _paged("projects", [], None))]}
        )
        client = TestRailClient(
            url=URL + "/", email="e", api_key="k", transport=transport
        )
        client.get_projects()
        self.assertEqual(
            str(transport.requests[0].url), f"{URL}/index.php?/api/v2/get_projects"
        )


class ProjectsAndSuitesTests(SimpleTestCase):
    def test_get_projects_maps_fields(self) -> None:
        payload = _paged(
            "projects",
            [{"id": 15, "name": "ExtJS 6", "suite_mode": 3, "is_completed": False}],
            None,
        )
        client = _client(RecordingTransport({"get_projects": [_json(200, payload)]}))
        self.assertEqual(
            client.get_projects(),
            [TestRailProject(id=15, name="ExtJS 6", suite_mode=3, is_completed=False)],
        )

    def test_get_suites_maps_fields(self) -> None:
        payload = _paged(
            "suites", [{"id": 6546, "name": "Master", "is_master": True}], None
        )
        client = _client(RecordingTransport({"get_suites/164": [_json(200, payload)]}))
        self.assertEqual(
            client.get_suites(164),
            [TestRailSuite(id=6546, name="Master", is_master=True)],
        )


class CasesPaginationTests(SimpleTestCase):
    def test_iter_cases_follows_next_links(self) -> None:
        first = _paged(
            "cases",
            [{"id": 1, "title": "A", "template_id": 1}],
            "/api/v2/get_cases/146&suite_id=1162&limit=250&offset=250",
        )
        second = _paged("cases", [{"id": 2, "title": "B", "template_id": 1}], None)
        transport = RecordingTransport(
            {
                "get_cases/146&suite_id=1162&limit=250": [_json(200, first)],
                "get_cases/146&suite_id=1162&limit=250&offset=250": [
                    _json(200, second)
                ],
            }
        )
        ids = [case.id for case in _client(transport).iter_cases(146, 1162)]
        self.assertEqual(ids, [1, 2])
        self.assertEqual(len(transport.requests), 2)

    def test_case_field_mapping_and_null_handling(self) -> None:
        raw = {
            "id": 338732,
            "title": "Modern: sort",
            "template_id": 3,
            "type_id": 6,
            "priority_id": None,
            "refs": None,
            "estimate": "5m",
            "custom_preconds": "<ol><li>x</li></ol>",
            "custom_steps": None,
            "custom_expected": "ok",
            "custom_steps_separated": [
                {"content": "Open", "expected": "Opens"},
                {"content": "Click", "expected": None},
            ],
        }
        payload = _paged("cases", [raw], None)
        transport = RecordingTransport(
            {"get_cases/146&suite_id=1162&limit=250": [_json(200, payload)]}
        )
        case = next(_client(transport).iter_cases(146, 1162))
        self.assertEqual(case.priority_id, None)
        self.assertEqual(case.refs, "")
        self.assertEqual(case.steps, "")
        self.assertEqual(case.expected, "ok")
        self.assertEqual(case.steps_separated[1].expected, "")
        self.assertIsInstance(case, TestRailCase)


class ErrorTests(SimpleTestCase):
    def test_401_raises_with_testrail_message(self) -> None:
        payload = {
            "error": "Authentication failed: invalid or missing user/password or session cookie."
        }
        client = _client(RecordingTransport({"get_projects": [_json(401, payload)]}))
        with self.assertRaises(TestRailError) as ctx:
            client.get_projects()
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("Authentication failed", ctx.exception.message)

    def test_non_json_body_raises(self) -> None:
        transport = httpx.MockTransport(lambda req: httpx.Response(200, text="<html>"))
        with self.assertRaises(TestRailError) as ctx:
            _client(transport).get_projects()
        self.assertEqual(ctx.exception.status_code, 200)

    def test_network_error_raises_status_zero(self) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with self.assertRaises(TestRailError) as ctx:
            _client(httpx.MockTransport(boom)).get_projects()
        self.assertEqual(ctx.exception.status_code, 0)

    def test_429_is_retried_with_retry_after_then_succeeds(self) -> None:
        sleeps: list[float] = []
        ok = _json(200, _paged("projects", [], None))
        transport = RecordingTransport(
            {
                "get_projects": [
                    _json(429, {"error": "slow down"}, {"Retry-After": "7"}),
                    ok,
                ]
            }
        )
        _client(transport, sleeps).get_projects()
        self.assertEqual(sleeps, [7.0])
        self.assertEqual(len(transport.requests), 2)

    def test_429_gives_up_after_max_retries(self) -> None:
        sleeps: list[float] = []
        limited = _json(429, {"error": "slow down"}, {"Retry-After": "1"})
        transport = RecordingTransport(
            {"get_projects": [limited, limited, limited, limited]}
        )
        with self.assertRaises(TestRailError) as ctx:
            _client(transport, sleeps).get_projects()
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(len(sleeps), 3)

    def test_retry_after_is_capped_at_ceiling(self) -> None:
        sleeps: list[float] = []
        ok = _json(200, _paged("projects", [], None))
        transport = RecordingTransport(
            {
                "get_projects": [
                    _json(429, {"error": "slow down"}, {"Retry-After": "300"}),
                    ok,
                ]
            }
        )
        _client(transport, sleeps).get_projects()
        self.assertEqual(sleeps, [30.0])


class RunTests(SimpleTestCase):
    def test_get_run_maps_fields(self) -> None:
        payload = {
            "id": 42,
            "name": "Punk Hazard: Nightly",
            "is_completed": False,
            "url": "https://sencha.testrail.com/index.php?/runs/view/42",
        }
        client = _client(RecordingTransport({"get_run/42": [_json(200, payload)]}))
        self.assertEqual(
            client.get_run(42),
            TestRailRun(
                id=42,
                name="Punk Hazard: Nightly",
                is_completed=False,
                url="https://sencha.testrail.com/index.php?/runs/view/42",
            ),
        )

    def test_get_run_404_raises_testrail_error(self) -> None:
        payload = {"error": "Field :run_id is not a valid test run."}
        client = _client(RecordingTransport({"get_run/999": [_json(404, payload)]}))
        with self.assertRaises(TestRailError) as ctx:
            client.get_run(999)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("not a valid test run", ctx.exception.message)

    def test_add_run_posts_expected_body_and_parses_run(self) -> None:
        response_payload = {
            "id": 7,
            "name": "Punk Hazard: Run 1",
            "is_completed": False,
            "url": "https://sencha.testrail.com/index.php?/runs/view/7",
        }
        transport = RecordingTransport({"add_run/3": [_json(200, response_payload)]})
        client = _client(transport)
        run = client.add_run(
            3,
            suite_id=12,
            name="Punk Hazard: Run 1",
            description="Run for project X",
            case_ids=[101, 102],
        )
        request = transport.requests[0]
        self.assertEqual(request.method, "POST")
        body = json.loads(request.content)
        self.assertEqual(
            body,
            {
                "suite_id": 12,
                "name": "Punk Hazard: Run 1",
                "description": "Run for project X",
                "include_all": False,
                "case_ids": [101, 102],
            },
        )
        self.assertEqual(
            run,
            TestRailRun(
                id=7,
                name="Punk Hazard: Run 1",
                is_completed=False,
                url="https://sencha.testrail.com/index.php?/runs/view/7",
            ),
        )

    def test_update_run_posts_expected_body(self) -> None:
        response_payload = {
            "id": 7,
            "name": "Punk Hazard: Run 1",
            "is_completed": False,
            "url": "",
        }
        transport = RecordingTransport({"update_run/7": [_json(200, response_payload)]})
        client = _client(transport)
        client.update_run(7, case_ids=[101, 102, 103])
        request = transport.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            json.loads(request.content),
            {"include_all": False, "case_ids": [101, 102, 103]},
        )


class AddResultsForCasesTests(SimpleTestCase):
    def test_posts_batch_omitting_elapsed_when_empty(self) -> None:
        response_payload = [
            {"id": 1, "status_id": 1},
            {"id": 2, "status_id": 5},
        ]
        transport = RecordingTransport(
            {"add_results_for_cases/9": [_json(200, response_payload)]}
        )
        client = _client(transport)
        results = client.add_results_for_cases(
            9,
            [
                TestRailResultInput(
                    case_id=101, status_id=1, comment="Passed.", elapsed=""
                ),
                TestRailResultInput(
                    case_id=102, status_id=5, comment="Failed.", elapsed="1h 2m 3s"
                ),
            ],
        )
        request = transport.requests[0]
        self.assertEqual(request.method, "POST")
        body = json.loads(request.content)
        self.assertEqual(
            body["results"],
            [
                {"case_id": 101, "status_id": 1, "comment": "Passed."},
                {
                    "case_id": 102,
                    "status_id": 5,
                    "comment": "Failed.",
                    "elapsed": "1h 2m 3s",
                },
            ],
        )
        self.assertNotIn("elapsed", body["results"][0])
        self.assertEqual(
            results,
            [TestRailResult(id=1, status_id=1), TestRailResult(id=2, status_id=5)],
        )

    def test_raises_when_response_length_differs_from_request(self) -> None:
        transport = RecordingTransport(
            {"add_results_for_cases/9": [_json(200, [{"id": 1, "status_id": 1}])]}
        )
        client = _client(transport)
        with self.assertRaises(TestRailError) as ctx:
            client.add_results_for_cases(
                9,
                [
                    TestRailResultInput(case_id=101, status_id=1, comment="Passed."),
                    TestRailResultInput(case_id=102, status_id=5, comment="Failed."),
                ],
            )
        self.assertEqual(ctx.exception.status_code, 200)
        self.assertIn("1 results", ctx.exception.message)
        self.assertIn("2 cases", ctx.exception.message)

    def test_empty_results_returns_empty_list_without_a_request(self) -> None:
        transport = RecordingTransport({})
        client = _client(transport)
        self.assertEqual(client.add_results_for_cases(9, []), [])
        self.assertEqual(transport.requests, [])


class PostTransportTests(SimpleTestCase):
    def test_post_429_is_retried_with_retry_after_then_succeeds(self) -> None:
        sleeps: list[float] = []
        ok = _json(200, {"id": 7, "name": "R", "is_completed": False, "url": ""})
        transport = RecordingTransport(
            {
                "update_run/7": [
                    _json(429, {"error": "slow down"}, {"Retry-After": "3"}),
                    ok,
                ]
            }
        )
        _client(transport, sleeps).update_run(7, case_ids=[1])
        self.assertEqual(sleeps, [3.0])
        self.assertEqual(len(transport.requests), 2)

    def test_post_400_surfaces_error_envelope_message(self) -> None:
        payload = {"error": "Field :case_ids is not a valid test case."}
        transport = RecordingTransport({"update_run/7": [_json(400, payload)]})
        client = _client(transport)
        with self.assertRaises(TestRailError) as ctx:
            client.update_run(7, case_ids=[1])
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("not a valid test case", ctx.exception.message)
