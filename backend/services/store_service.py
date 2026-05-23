from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
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


@dataclass(frozen=True)
class QuotaCounterKey:
    key: str
    limit: int


@dataclass
class QuotaCounterSnapshot:
    key: str
    limit: int
    used: int
    remaining: int


@dataclass
class QuotaReservationResult:
    allowed: bool
    reservation_key: str
    amount: int
    exceeded_key: str | None = None
    snapshots: list[QuotaCounterSnapshot] | None = None


@dataclass
class QuotaReservationRecord:
    keys: list[str]
    amount: int
    expires_at: datetime | None
    reconciled: bool = False
    released: bool = False


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

    @abstractmethod
    def reserve_quota(
        self,
        keys: list[QuotaCounterKey],
        reservation_key: str,
        amount: int,
        ttl_seconds: int,
    ) -> QuotaReservationResult: ...

    @abstractmethod
    def release_quota(self, reservation_key: str) -> None: ...

    @abstractmethod
    def reconcile_quota(self, reservation_key: str, actual_amount: int) -> QuotaReservationResult: ...

    @abstractmethod
    def quota_snapshot(self, keys: list[QuotaCounterKey]) -> list[QuotaCounterSnapshot]: ...

    def health_snapshot(self) -> dict[str, str | bool]:
        return {"backend": self.backend_name, "distributed": self.is_distributed}


class MemoryStore(BaseStore):
    backend_name = "memory"
    is_distributed = False

    def __init__(self) -> None:
        self._values: dict[str, StoredValue] = {}
        self._sets: dict[str, set[str]] = {}
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._quota_counters: dict[str, int] = defaultdict(int)
        self._quota_expiries: dict[str, datetime] = {}
        self._quota_reservations: dict[str, QuotaReservationRecord] = {}
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

    def _cleanup_quota_key(self, key: str) -> None:
        expires_at = self._quota_expiries.get(key)
        if expires_at and expires_at <= datetime.now(UTC):
            self._quota_counters.pop(key, None)
            self._quota_expiries.pop(key, None)

    def _cleanup_reservation(self, reservation_key: str) -> None:
        record = self._quota_reservations.get(reservation_key)
        if record and record.expires_at and record.expires_at <= datetime.now(UTC):
            self._quota_reservations.pop(reservation_key, None)

    def reserve_quota(
        self,
        keys: list[QuotaCounterKey],
        reservation_key: str,
        amount: int,
        ttl_seconds: int,
    ) -> QuotaReservationResult:
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        with self._lock:
            self._cleanup_reservation(reservation_key)
            existing = self._quota_reservations.get(reservation_key)
            if existing and not existing.released:
                snapshots = self._quota_snapshot_unlocked(keys)
                return QuotaReservationResult(True, reservation_key, existing.amount, snapshots=snapshots)

            for item in keys:
                self._cleanup_quota_key(item.key)
                if self._quota_counters[item.key] + amount > item.limit:
                    snapshots = [
                        QuotaCounterSnapshot(
                            key=key.key,
                            limit=key.limit,
                            used=self._quota_counters[key.key],
                            remaining=max(0, key.limit - self._quota_counters[key.key]),
                        )
                        for key in keys
                    ]
                    return QuotaReservationResult(
                        False,
                        reservation_key,
                        amount,
                        exceeded_key=item.key,
                        snapshots=snapshots,
                    )

            for item in keys:
                self._quota_counters[item.key] += amount
                self._quota_expiries[item.key] = expires_at
            self._quota_reservations[reservation_key] = QuotaReservationRecord(
                keys=[item.key for item in keys],
                amount=amount,
                expires_at=expires_at,
            )
            return QuotaReservationResult(True, reservation_key, amount, snapshots=self._quota_snapshot_unlocked(keys))

    def release_quota(self, reservation_key: str) -> None:
        with self._lock:
            record = self._quota_reservations.get(reservation_key)
            if not record or record.released or record.reconciled:
                return
            for key in record.keys:
                self._cleanup_quota_key(key)
                self._quota_counters[key] = max(0, self._quota_counters[key] - record.amount)
            record.released = True

    def reconcile_quota(self, reservation_key: str, actual_amount: int) -> QuotaReservationResult:
        with self._lock:
            record = self._quota_reservations.get(reservation_key)
            if not record or record.released:
                return QuotaReservationResult(False, reservation_key, actual_amount, exceeded_key=reservation_key)
            if record.reconciled:
                keys = [QuotaCounterKey(key=key, limit=max(self._quota_counters.get(key, 0), actual_amount)) for key in record.keys]
                return QuotaReservationResult(True, reservation_key, record.amount, snapshots=self._quota_snapshot_unlocked(keys))
            delta = actual_amount - record.amount
            for key in record.keys:
                self._cleanup_quota_key(key)
                self._quota_counters[key] = max(0, self._quota_counters[key] + delta)
            record.amount = actual_amount
            record.reconciled = True
            keys = [QuotaCounterKey(key=key, limit=max(self._quota_counters.get(key, 0), actual_amount)) for key in record.keys]
            return QuotaReservationResult(True, reservation_key, actual_amount, snapshots=self._quota_snapshot_unlocked(keys))

    def _quota_snapshot_unlocked(self, keys: list[QuotaCounterKey]) -> list[QuotaCounterSnapshot]:
        snapshots: list[QuotaCounterSnapshot] = []
        for item in keys:
            self._cleanup_quota_key(item.key)
            used = self._quota_counters.get(item.key, 0)
            snapshots.append(
                QuotaCounterSnapshot(
                    key=item.key,
                    limit=item.limit,
                    used=used,
                    remaining=max(0, item.limit - used),
                )
            )
        return snapshots

    def quota_snapshot(self, keys: list[QuotaCounterKey]) -> list[QuotaCounterSnapshot]:
        with self._lock:
            return self._quota_snapshot_unlocked(keys)


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
    _RESERVE_QUOTA_SCRIPT = """
    local reservation_key = KEYS[1]
    local amount = tonumber(ARGV[1])
    local ttl = tonumber(ARGV[2])
    local existing = redis.call('GET', reservation_key)
    if existing then
      return {1, '', existing}
    end
    local key_count = tonumber(ARGV[3])
    for i = 1, key_count do
      local counter_key = KEYS[i + 1]
      local limit = tonumber(ARGV[3 + i])
      local current = tonumber(redis.call('GET', counter_key) or '0')
      if current + amount > limit then
        return {0, counter_key, ''}
      end
    end
    local keys = {}
    for i = 1, key_count do
      local counter_key = KEYS[i + 1]
      redis.call('INCRBY', counter_key, amount)
      redis.call('EXPIRE', counter_key, ttl)
      keys[i] = counter_key
    end
    local payload = cjson.encode({keys=keys, amount=amount, reconciled=false, released=false})
    redis.call('SET', reservation_key, payload, 'EX', ttl)
    return {1, '', payload}
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
        self._reserve_quota_script = self.client.register_script(self._RESERVE_QUOTA_SCRIPT)

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

    def reserve_quota(
        self,
        keys: list[QuotaCounterKey],
        reservation_key: str,
        amount: int,
        ttl_seconds: int,
    ) -> QuotaReservationResult:
        redis_keys = [self._key(reservation_key), *[self._key(item.key) for item in keys]]
        args = [amount, ttl_seconds, len(keys), *[item.limit for item in keys]]
        result = self._reserve_quota_script(keys=redis_keys, args=args)
        allowed = bool(int(result[0]))
        exceeded_key = str(result[1]) if result[1] else None
        snapshots = self.quota_snapshot(keys)
        return QuotaReservationResult(allowed, reservation_key, amount, exceeded_key=exceeded_key, snapshots=snapshots)

    def _reservation_payload(self, reservation_key: str) -> dict | None:
        raw = self.client.get(self._key(reservation_key))
        if not raw:
            return None
        return json.loads(str(raw))

    def _write_reservation_payload(self, reservation_key: str, payload: dict) -> None:
        ttl = self.client.ttl(self._key(reservation_key))
        self.client.set(self._key(reservation_key), json.dumps(payload), ex=ttl if ttl and ttl > 0 else None)

    def release_quota(self, reservation_key: str) -> None:
        payload = self._reservation_payload(reservation_key)
        if not payload or payload.get("released") or payload.get("reconciled"):
            return
        amount = int(payload.get("amount") or 0)
        pipe = self.client.pipeline()
        for key in payload.get("keys") or []:
            pipe.decrby(key, amount)
        payload["released"] = True
        self._write_reservation_payload(reservation_key, payload)
        pipe.execute()

    def reconcile_quota(self, reservation_key: str, actual_amount: int) -> QuotaReservationResult:
        payload = self._reservation_payload(reservation_key)
        if not payload or payload.get("released"):
            return QuotaReservationResult(False, reservation_key, actual_amount, exceeded_key=reservation_key)
        if payload.get("reconciled"):
            return QuotaReservationResult(True, reservation_key, int(payload.get("amount") or 0))
        reserved_amount = int(payload.get("amount") or 0)
        delta = actual_amount - reserved_amount
        pipe = self.client.pipeline()
        for key in payload.get("keys") or []:
            pipe.incrby(key, delta)
        payload["amount"] = actual_amount
        payload["reconciled"] = True
        self._write_reservation_payload(reservation_key, payload)
        pipe.execute()
        return QuotaReservationResult(True, reservation_key, actual_amount)

    def quota_snapshot(self, keys: list[QuotaCounterKey]) -> list[QuotaCounterSnapshot]:
        if not keys:
            return []
        values = self.client.mget([self._key(item.key) for item in keys])
        snapshots: list[QuotaCounterSnapshot] = []
        for item, raw in zip(keys, values, strict=True):
            used = int(raw or 0)
            snapshots.append(QuotaCounterSnapshot(item.key, item.limit, used, max(0, item.limit - used)))
        return snapshots

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
