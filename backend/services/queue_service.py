from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass

from backend.config import get_settings


class QueueFullError(RuntimeError):
    pass


@dataclass
class QueueTicket:
    waiting_before: int


class QueueService:
    def __init__(self) -> None:
        settings = get_settings()
        self._max_waiting = settings.queue_max_waiting
        self._waiting = 0
        self._semaphore = asyncio.Semaphore(settings.queue_max_concurrent)
        self._lock = asyncio.Lock()

    @property
    def waiting(self) -> int:
        return self._waiting

    def ensure_available(self) -> None:
        current_active = self._semaphore._value  # noqa: SLF001
        if self._waiting + (0 if current_active > 0 else 1) >= self._max_waiting:
            raise QueueFullError("queue_full")

    @asynccontextmanager
    async def reserve(self) -> QueueTicket:
        async with self._lock:
            current_active = self._semaphore._value  # noqa: SLF001
            if self._waiting + (0 if current_active > 0 else 1) >= self._max_waiting:
                raise QueueFullError("queue_full")
            ticket = QueueTicket(waiting_before=self._waiting)
            self._waiting += 1

        try:
            await self._semaphore.acquire()
        finally:
            # 排队中被取消（客户端断开）时也要退出等待计数；此时未拿到槽位，不能 release
            self._waiting -= 1
        try:
            yield ticket
        finally:
            self._semaphore.release()


queue_service = QueueService()
