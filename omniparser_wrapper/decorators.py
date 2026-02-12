from __future__ import annotations

import functools
from typing import Any, Callable

from django.conf import settings
from django.http import HttpRequest, JsonResponse


def require_api_key(view_func: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(view_func)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
        api_key = request.headers.get("X-API-Key", "")
        expected_key: str = settings.OMNIPARSER_API_KEY
        if not expected_key or api_key != expected_key:
            return JsonResponse({"error": "Invalid or missing API key"}, status=401)
        return view_func(request, *args, **kwargs)

    return wrapper
