from __future__ import annotations

from threading import Lock

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.requests import Request

chat_request_total = Counter("chat_request_total", "Total chat requests")
chat_first_token_seconds = Histogram("chat_first_token_seconds", "Latency until first streamed token")
chat_full_response_seconds = Histogram("chat_full_response_seconds", "Full response latency")
chat_stream_disconnect_total = Counter("chat_stream_disconnect_total", "Disconnected chat streams")
chat_stream_safety_rewrite_total = Counter(
    "chat_stream_safety_rewrite_total",
    "Stream responses rewritten due to unsafe output",
)
sse_active_connections = Gauge("sse_active_connections", "Active SSE connections")
llm_queue_depth = Gauge("llm_queue_depth", "Current chat waiting queue depth")
filter_blocked_total = Counter("filter_blocked_total", "Blocked non-subject requests")
guidance_stage_total = Counter("guidance_stage_total", "Guidance stage count", ["stage"])
celery_task_duration_seconds = Histogram("celery_task_duration_seconds", "Knowledge ingest task duration")
http_request_total = Counter("http_request_total", "Total HTTP requests", ["method", "path", "status_code"])
http_request_duration_seconds = Histogram("http_request_duration_seconds", "HTTP request latency", ["method", "path"])
rate_limit_rejected_total = Counter("rate_limit_rejected_total", "Rate-limited HTTP requests", ["path"])
chat_cache_hit_total = Counter("chat_cache_hit_total", "Hot question cache hits")
chat_cache_miss_total = Counter("chat_cache_miss_total", "Hot question cache misses")
cache_hit_rate = Gauge("cache_hit_rate", "Hot question cache hit rate")

_cache_lookup_lock = Lock()
_cache_hits = 0
_cache_lookups = 0


def resolve_path_label(request: Request) -> str:
    route = request.scope.get("route")
    for attr in ("path_format", "path"):
        value = getattr(route, attr, None)
        if value:
            return str(value)
    return request.url.path or "unknown"


def observe_http_request(method: str, path: str, status_code: int, duration_seconds: float) -> None:
    normalized_path = path or "unknown"
    normalized_method = method.upper() if method else "UNKNOWN"
    http_request_total.labels(method=normalized_method, path=normalized_path, status_code=str(status_code)).inc()
    http_request_duration_seconds.labels(method=normalized_method, path=normalized_path).observe(duration_seconds)


def record_rate_limit_rejection(path: str) -> None:
    rate_limit_rejected_total.labels(path=path or "unknown").inc()


def record_chat_cache_lookup(hit: bool) -> None:
    global _cache_hits, _cache_lookups

    with _cache_lookup_lock:
        _cache_lookups += 1
        if hit:
            _cache_hits += 1
            chat_cache_hit_total.inc()
        else:
            chat_cache_miss_total.inc()
        cache_hit_rate.set(_cache_hits / _cache_lookups if _cache_lookups else 0.0)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
