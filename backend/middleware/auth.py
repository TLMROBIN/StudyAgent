from __future__ import annotations

import logging
from time import perf_counter
from uuid import uuid4

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from backend.observability import reset_request_id, set_request_id
from backend.services.metrics_service import observe_http_request, resolve_path_label

logger = logging.getLogger("studyagent.request")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.request_id = request.headers.get("X-Request-ID") or str(uuid4())
        token = set_request_id(request.state.request_id)
        started = perf_counter()
        response = None

        try:
            response = await call_next(request)
            return response
        except Exception:
            duration_seconds = perf_counter() - started
            path = resolve_path_label(request)
            observe_http_request(request.method, path, 500, duration_seconds)
            logger.exception(
                "request failed",
                extra={
                    "request_id": request.state.request_id,
                    "method": request.method,
                    "path": path,
                    "status_code": 500,
                    "duration_ms": round(duration_seconds * 1000, 2),
                    "client_ip": request.client.host if request.client else None,
                    "user_agent": request.headers.get("user-agent"),
                },
            )
            raise
        finally:
            if response is not None:
                duration_seconds = perf_counter() - started
                path = resolve_path_label(request)
                observe_http_request(request.method, path, response.status_code, duration_seconds)
                response.headers["X-Request-ID"] = request.state.request_id
                logger.info(
                    "request completed",
                    extra={
                        "request_id": request.state.request_id,
                        "method": request.method,
                        "path": path,
                        "status_code": response.status_code,
                        "duration_ms": round(duration_seconds * 1000, 2),
                        "client_ip": request.client.host if request.client else None,
                        "user_agent": request.headers.get("user-agent"),
                    },
                )
            reset_request_id(token)
