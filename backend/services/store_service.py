from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
from threading import Lock
from time import time
from uuid import uuid4

import redis

from backend.config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass
class StoredValue:
    value: str
    expires_at: datetime | None = None


class BaseStore(ABC):
    backend_name = "base"
    is_distributed = False

    @abstractmethod
    def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None: ...

    @abstractmethod
    def get(self, key: str) -> str | None: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def sadd(self, key: str, value: str) -> None: ...

    @abstractmethod
    def srem(self, key: str, value: str) -> None: ...

    @abstractmethod
    def smembers(self, key: str) -> set[str]: ...

    @abstractmethod
    def delete_set(self, key: str) -> None: ...

    @abstractmethod
    def hit_sliding_window(self, key: str, limit: int, window_seconds: int) -> bool: ...

    def health_snapshot(self) -> dict[str, str | bool]:
        return {"backend": self.backend_name, "distributed": self.is_distributed}


class MemoryStore(BaseStore):
    backend_name = "memory"
    is_distributed = False

    def __init__(self) -> None:
        self._values: dict[str, StoredValue] = {}
        self._sets: dict[str, set[str]] = {}
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def _cleanup_if_expired(self, key: str) -> None:
        stored = self._values.get(key)
        if not stored or not stored.expires_at:
            return
        if stored.expires_at <= datetime.now(UTC):
            self._values.pop(key, None)

    def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds) if ttl_seconds else None
        with self._lock:
            self._values[key] = StoredValue(value=value, expires_at=expires_at)

    def get(self, key: str) -> str | None:
        with self._lock:
            self._cleanup_if_expired(key)
            stored = self._values.get(key)
            return stored.value if stored else None

    def delete(self, key: str) -> None:
        with self._lock:
            self._values.pop(key, None)

    def sadd(self, key: str, value: str) -> None:
        with self._lock:
            self._sets.setdefault(key, set()).add(value)

    def srem(self, key: str, value: str) -> None:
        with self._lock:
            if key in self._sets:
                self._sets[key].discard(value)

    def smembers(self, key: str) -> set[str]:
        with self._lock:
            return set(self._sets.get(key, set()))

    def delete_set(self, key: str) -> None:
        with self._lock:
            self._sets.pop(key, None)

    def hit_sliding_window(self, key: str, limit: int, window_seconds: int) -> bool:
        with self._lock:
            bucket = self._hits[key]
            now = time()
            while bucket and now - bucket[0] > window_seconds:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            return True


class RedisStore(BaseStore):
    backend_name = "redis"
    is_distributed = True
    _RATE_LIMIT_SCRIPT = """
    local key = KEYS[1]
    local now_ms = tonumber(ARGV[1])
    local window_ms = tonumber(ARGV[2])
    local limit = tonumber(ARGV[3])
    local member = ARGV[4]
    redis.call('ZREMRANGEBYSCORE', key, 0, now_ms - window_ms)
    local count = redis.call('ZCARD', key)
    if count >= limit then
      redis.call('EXPIRE', key, math.max(1, math.ceil(window_ms / 1000)))
      return 0
    end
    redis.call('ZADD', key, now_ms, member)
    redis.call('EXPIRE', key, math.max(1, math.ceil(window_ms / 1000)))
    return 1
    """

    def __init__(self, redis_url: str, prefix: str, connect_timeout_seconds: float = 1.0) -> None:
        self.prefix = prefix
        self.client = redis.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=connect_timeout_seconds,
            socket_timeout=connect_timeout_seconds,
        )
        self._rate_limit_script = self.client.register_script(self._RATE_LIMIT_SCRIPT)

    def ping(self) -> None:
        self.client.ping()

    def _key(self, key: str) -> str:
        return f"{self.prefix}:{key}"

    def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        self.client.set(self._key(key), value, ex=ttl_seconds)

    def get(self, key: str) -> str | None:
        value = self.client.get(self._key(key))
        return str(value) if value is not None else None

    def delete(self, key: str) -> None:
        self.client.delete(self._key(key))

    def sadd(self, key: str, value: str) -> None:
        self.client.sadd(self._key(key), value)

    def srem(self, key: str, value: str) -> None:
        self.client.srem(self._key(key), value)

    def smembers(self, key: str) -> set[str]:
        return {str(item) for item in self.client.smembers(self._key(key))}

    def delete_set(self, key: str) -> None:
        self.client.delete(self._key(key))

    def hit_sliding_window(self, key: str, limit: int, window_seconds: int) -> bool:
        now_ms = int(time() * 1000)
        member = f"{now_ms}:{uuid4().hex}"
        result = self._rate_limit_script(
            keys=[self._key(f"ratelimit:{key}")],
            args=[now_ms, int(window_seconds * 1000), limit, member],
        )
        return bool(int(result))

    def health_snapshot(self) -> dict[str, str | bool]:
        return {
            "backend": self.backend_name,
            "distributed": self.is_distributed,
            "prefix": self.prefix,
        }


def build_store(settings: Settings | None = None) -> BaseStore:
    settings = settings or get_settings()
    try:
        redis_store = RedisStore(
            redis_url=settings.redis_url,
            prefix=settings.redis_key_prefix,
            connect_timeout_seconds=settings.redis_connect_timeout_seconds,
        )
        redis_store.ping()
        logger.info("Using Redis-backed store")
        return redis_store
    except Exception as exc:
        logger.warning("Redis unavailable, falling back to in-memory store: %s", exc)
        return MemoryStore()


store = build_store()
