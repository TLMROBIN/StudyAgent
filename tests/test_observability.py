import asyncio
import json
import logging

from backend.middleware.auth import RequestContextMiddleware
from backend.observability import JsonFormatter
from starlette.requests import Request
from starlette.responses import JSONResponse


def test_json_formatter_serializes_structured_fields():
    formatter = JsonFormatter()
    record = logging.LogRecord("studyagent.request", logging.INFO, __file__, 10, "request completed", (), None)
    record.request_id = "req-1"
    record.method = "GET"
    record.path = "/health"
    record.status_code = 200
    record.duration_ms = 12.5
    payload = json.loads(formatter.format(record))

    assert payload["request_id"] == "req-1"
    assert payload["method"] == "GET"
    assert payload["path"] == "/health"
    assert payload["status_code"] == 200
    assert payload["duration_ms"] == 12.5


def test_request_context_middleware_sets_request_id_header():
    middleware = RequestContextMiddleware(app=lambda scope, receive, send: None)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/ping",
        "raw_path": b"/ping",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 5000),
        "server": ("testserver", 80),
        "root_path": "",
        "app": None,
    }
    request = Request(scope)

    async def call_next(_request: Request):
        return JSONResponse({"status": "ok"})

    response = asyncio.run(middleware.dispatch(request, call_next))

    assert response.status_code == 200
    assert response.headers["X-Request-ID"]
