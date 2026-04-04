from __future__ import annotations

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from backend.services.metrics_service import record_rate_limit_rejection
from backend.services.store_service import store


class SlidingWindowRateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, limit: int = 120, window_seconds: int = 60):
        super().__init__(app)
        self.limit = limit
        self.window_seconds = window_seconds

    async def dispatch(self, request: Request, call_next):
        key = request.client.host if request.client else "unknown"
        path_key = request.url.path.replace("/", "_") or "root"
        scoped_key = f"{key}:{path_key}"
        if not store.hit_sliding_window(scoped_key, limit=self.limit, window_seconds=self.window_seconds):
            record_rate_limit_rejection(request.url.path)
            return JSONResponse(status_code=429, content={"detail": "Too many requests"})
        return await call_next(request)
