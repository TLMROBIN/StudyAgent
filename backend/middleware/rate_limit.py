from __future__ import annotations

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Match
from starlette.responses import JSONResponse

from backend.security import decode_token
from backend.services.metrics_service import record_rate_limit_rejection
from backend.services.store_service import store


class SlidingWindowRateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, limit: int = 120, window_seconds: int = 60):
        super().__init__(app)
        self.limit = limit
        self.window_seconds = window_seconds

    async def dispatch(self, request: Request, call_next):
        key = _rate_limit_identity(request)
        path_key = request.url.path.replace("/", "_") or "root"
        scoped_key = f"{key}:{path_key}"
        if not store.hit_sliding_window(scoped_key, limit=self.limit, window_seconds=self.window_seconds):
            record_rate_limit_rejection(_route_label(request))
            return JSONResponse(status_code=429, content={"detail": "Too many requests"})
        return await call_next(request)


def _rate_limit_identity(request: Request) -> str:
    # 班级 WiFi 可能整班共用一个出口 IP，有效 access token 按账号计数；只校验签名不查库，
    # 伪造不了别人的账号，被吊销的 token 也只会消耗它自己账号的额度
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token:
        try:
            payload = decode_token(token.strip())
        except ValueError:
            payload = None
        if payload and payload.get("type") == "access" and payload.get("sub"):
            return f"user:{payload['sub']}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


def _route_label(request: Request) -> str:
    # 限流在路由之前执行，这里按已注册路由模板归一化标签，避免原始路径撑爆指标基数
    for route in request.app.router.routes:
        match, _ = route.matches(request.scope)
        if match == Match.FULL:
            return str(getattr(route, "path_format", None) or getattr(route, "path", "unmatched"))
    return "unmatched"
