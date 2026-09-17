from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

TESTRAIL_PAGE_SIZE = 250
TESTRAIL_MAX_RATE_LIMIT_RETRIES = 3
TESTRAIL_MAX_RETRY_AFTER_SECONDS = 30.0
_DEFAULT_RETRY_AFTER_SECONDS = 5.0
_API_PREFIX = "/index.php?/api/v2/"


class TestRailError(Exception):
    """Any failure talking to TestRail. status_code 0 means no HTTP response."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class TestRailProject:
    id: int
    name: str
    suite_mode: int
    is_completed: bool


@dataclass(frozen=True)
class TestRailSuite:
    id: int
    name: str
    is_master: bool


@dataclass(frozen=True)
class TestRailCaseType:
    id: int
    name: str


@dataclass(frozen=True)
class TestRailPriority:
    id: int
    name: str


@dataclass(frozen=True)
class TestRailStep:
    content: str
    expected: str


@dataclass(frozen=True)
class TestRailCase:
    id: int
    title: str
    template_id: int
    type_id: int | None
    priority_id: int | None
    refs: str
    estimate: str
    preconditions: str
    steps: str
    expected: str
    steps_separated: tuple[TestRailStep, ...]


@dataclass(frozen=True)
class TestRailRun:
    id: int
    name: str
    is_completed: bool
    url: str


@dataclass(frozen=True)
class TestRailResultInput:
    case_id: int
    status_id: int
    comment: str
    elapsed: str = ""


@dataclass(frozen=True)
class TestRailResult:
    id: int
    status_id: int


def _str(value: Any) -> str:  # raw JSON field value, type varies by payload
    return "" if value is None else str(value)


def _optional_int(value: Any) -> int | None:  # raw JSON field, type varies by payload
    return None if value is None else int(value)


def _parse_steps_separated(
    raw: Any,  # raw JSON field, expected to be a list of step dicts
) -> tuple[TestRailStep, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(
        TestRailStep(
            content=_str(item.get("content")), expected=_str(item.get("expected"))
        )
        for item in raw
        if isinstance(item, dict)
    )


def _parse_case(raw: dict[str, Any]) -> TestRailCase:  # raw JSON case object
    return TestRailCase(
        id=int(raw["id"]),
        title=_str(raw.get("title")),
        template_id=int(raw.get("template_id") or 0),
        type_id=_optional_int(raw.get("type_id")),
        priority_id=_optional_int(raw.get("priority_id")),
        refs=_str(raw.get("refs")),
        estimate=_str(raw.get("estimate")),
        preconditions=_str(raw.get("custom_preconds")),
        steps=_str(raw.get("custom_steps")),
        expected=_str(raw.get("custom_expected")),
        steps_separated=_parse_steps_separated(raw.get("custom_steps_separated")),
    )


def _parse_run(raw: dict[str, Any]) -> TestRailRun:  # raw JSON run object
    return TestRailRun(
        id=int(raw["id"]),
        name=_str(raw.get("name")),
        is_completed=bool(raw.get("is_completed")),
        url=_str(raw.get("url")),
    )


def _parse_result(raw: dict[str, Any]) -> TestRailResult:  # raw JSON result object
    return TestRailResult(
        id=int(raw["id"]),
        status_id=int(raw.get("status_id") or 0),
    )


def _result_input_payload(
    result: TestRailResultInput,
) -> dict[str, Any]:  # request body fragment, TestRail expects untyped JSON
    payload: dict[str, Any] = {
        "case_id": result.case_id,
        "status_id": result.status_id,
        "comment": result.comment,
    }
    if result.elapsed:
        payload["elapsed"] = result.elapsed
    return payload


class TestRailClient:
    """Typed TestRail API v2 client.

    GET methods support importing projects, suites and cases. POST methods
    support pushing a Punk Hazard test run's results back to a TestRail run.
    """

    def __init__(
        self,
        *,
        url: str,
        email: str,
        api_key: str,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base_url = url.rstrip("/")
        self._sleep = sleep
        self._http = httpx.Client(
            auth=(email, api_key),
            headers={"Content-Type": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    def __enter__(self) -> TestRailClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # -- public read methods -------------------------------------------------

    def get_projects(self) -> list[TestRailProject]:
        return [
            TestRailProject(
                id=int(raw["id"]),
                name=_str(raw.get("name")),
                suite_mode=int(raw.get("suite_mode") or 1),
                is_completed=bool(raw.get("is_completed")),
            )
            for raw in self._paginate("get_projects", "projects")
        ]

    def get_suites(self, project_id: int) -> list[TestRailSuite]:
        return [
            TestRailSuite(
                id=int(raw["id"]),
                name=_str(raw.get("name")),
                is_master=bool(raw.get("is_master")),
            )
            for raw in self._paginate(f"get_suites/{project_id}", "suites")
        ]

    def iter_cases(self, project_id: int, suite_id: int) -> Iterator[TestRailCase]:
        method = (
            f"get_cases/{project_id}&suite_id={suite_id}&limit={TESTRAIL_PAGE_SIZE}"
        )
        for raw in self._paginate(method, "cases"):
            yield _parse_case(raw)

    def get_case_types(self) -> list[TestRailCaseType]:
        return [
            TestRailCaseType(id=int(raw["id"]), name=_str(raw.get("name")))
            for raw in self._get_list("get_case_types")
        ]

    def get_priorities(self) -> list[TestRailPriority]:
        return [
            TestRailPriority(id=int(raw["id"]), name=_str(raw.get("name")))
            for raw in self._get_list("get_priorities")
        ]

    # -- public write methods -------------------------------------------------

    def get_run(self, run_id: int) -> TestRailRun:
        """Fetch a single TestRail run, e.g. to check whether it is still open."""
        return _parse_run(self._get_dict(f"get_run/{run_id}"))

    def add_run(
        self,
        project_id: int,
        *,
        suite_id: int,
        name: str,
        description: str,
        case_ids: Sequence[int],
    ) -> TestRailRun:
        """Create a new TestRail run scoped to the given case ids."""
        body: dict[str, Any] = {  # request body, TestRail expects untyped JSON
            "suite_id": suite_id,
            "name": name,
            "description": description,
            "include_all": False,
            "case_ids": list(case_ids),
        }
        return _parse_run(self._post_dict(f"add_run/{project_id}", body))

    def update_run(self, run_id: int, *, case_ids: Sequence[int]) -> TestRailRun:
        """Replace the case set of an existing TestRail run."""
        body: dict[str, Any] = {  # request body, TestRail expects untyped JSON
            "include_all": False,
            "case_ids": list(case_ids),
        }
        return _parse_run(self._post_dict(f"update_run/{run_id}", body))

    def add_results_for_cases(
        self, run_id: int, results: Sequence[TestRailResultInput]
    ) -> list[TestRailResult]:
        """Append results for cases in a run. Results are immutable in TestRail:
        every call adds new results rather than updating existing ones."""
        if not results:
            return []
        body = {"results": [_result_input_payload(result) for result in results]}
        raw_results = self._post_list(f"add_results_for_cases/{run_id}", body)
        if len(raw_results) != len(results):
            raise TestRailError(
                f"TestRail returned {len(raw_results)} results "
                f"for {len(results)} cases.",
                200,
            )
        return [_parse_result(raw) for raw in raw_results]

    # -- transport helpers ---------------------------------------------------

    def _paginate(
        self, method: str, key: str
    ) -> Iterator[dict[str, Any]]:  # yields raw JSON objects, one per page item
        next_method: str | None = method
        while next_method is not None:
            page = self._get_dict(next_method)
            items = page.get(key)
            if not isinstance(items, list):
                raise TestRailError(
                    f"TestRail response is missing the '{key}' list.", 200
                )
            for item in items:
                if isinstance(item, dict):
                    yield item
            next_method = self._next_method(page)

    @staticmethod
    def _next_method(page: dict[str, Any]) -> str | None:  # raw JSON page envelope
        links = page.get("_links")
        if not isinstance(links, dict):
            return None
        next_link = links.get("next")
        if not isinstance(next_link, str) or not next_link:
            return None
        return next_link.removeprefix("/api/v2/")

    def _get_dict(
        self, method: str
    ) -> dict[str, Any]:  # raw JSON object, keys vary by endpoint
        data = self._get_json(method)
        if not isinstance(data, dict):
            raise TestRailError("TestRail returned an unexpected response shape.", 200)
        return data

    def _get_list(
        self, method: str
    ) -> list[dict[str, Any]]:  # raw JSON objects, keys vary by endpoint
        data = self._get_json(method)
        if not isinstance(data, list):
            raise TestRailError("TestRail returned an unexpected response shape.", 200)
        return [item for item in data if isinstance(item, dict)]

    def _post_dict(
        self, method: str, body: dict[str, Any]
    ) -> dict[str, Any]:  # raw JSON object, keys vary by endpoint
        data = self._post_json(method, body)
        if not isinstance(data, dict):
            raise TestRailError("TestRail returned an unexpected response shape.", 200)
        return data

    def _post_list(
        self, method: str, body: dict[str, Any]
    ) -> list[dict[str, Any]]:  # raw JSON objects, keys vary by endpoint
        data = self._post_json(method, body)
        if not isinstance(data, list):
            raise TestRailError("TestRail returned an unexpected response shape.", 200)
        return [item for item in data if isinstance(item, dict)]

    def _get_json(self, method: str) -> Any:  # JSON payloads are untyped by nature
        return self._parse_json_response(self._request_with_rate_limit_retry(method))

    def _post_json(
        self, method: str, body: dict[str, Any]
    ) -> Any:  # JSON payloads are untyped by nature
        return self._parse_json_response(
            self._request_with_rate_limit_retry(method, body)
        )

    def _parse_json_response(
        self, response: httpx.Response
    ) -> Any:  # JSON payloads are untyped by nature
        if response.status_code >= 400:
            raise TestRailError(self._error_message(response), response.status_code)
        try:
            return response.json()
        except ValueError as exc:
            raise TestRailError(
                "TestRail returned a non-JSON response.", response.status_code
            ) from exc

    def _request_with_rate_limit_retry(
        self, method: str, json_body: dict[str, Any] | None = None
    ) -> httpx.Response:
        url = f"{self._base_url}{_API_PREFIX}{method}"
        attempts = 0
        while True:
            try:
                response = self._send_request(url, json_body)
            except httpx.HTTPError as exc:
                raise TestRailError(f"Could not reach TestRail: {exc}", 0) from exc
            if (
                response.status_code != 429
                or attempts >= TESTRAIL_MAX_RATE_LIMIT_RETRIES
            ):
                return response
            attempts += 1
            self._sleep(self._retry_after_seconds(response))

    def _send_request(
        self, url: str, json_body: dict[str, Any] | None
    ) -> httpx.Response:
        if json_body is None:
            return self._http.get(url)
        return self._http.post(url, json=json_body)

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float:
        header = response.headers.get("Retry-After")
        try:
            parsed = float(header) if header else _DEFAULT_RETRY_AFTER_SECONDS
        except ValueError:
            return _DEFAULT_RETRY_AFTER_SECONDS
        return min(parsed, TESTRAIL_MAX_RETRY_AFTER_SECONDS)

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return f"TestRail returned HTTP {response.status_code}."
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, str):
                return error
        return f"TestRail returned HTTP {response.status_code}."
