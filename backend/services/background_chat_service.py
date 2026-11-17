"""Keep generation alive independently of an SSE subscriber (single backend worker)."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
import json
import logging
from time import monotonic

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from starlette.datastructures import State
from backend.services.metrics_service import chat_stream_disconnect_total, sse_active_connections

logger = logging.getLogger(__name__)


@dataclass
class BackgroundChat:
    fingerprint: str
    ready: asyncio.Future
    frames: list[str | bytes] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    finished_at: float | None = None
    request_state: State | None = None

    async def subscribe(self) -> AsyncIterator[str | bytes]:
        # Cancelling a subscriber must never cancel the producer or its ready future.
        await asyncio.shield(self.ready)
        cursor = 0
        sse_active_connections.inc()
        try:
            while True:
                self.changed.clear()
                while cursor < len(self.frames):
                    frame = self.frames[cursor]
                    cursor += 1
                    yield frame
                if self.finished_at is not None:
                    return
                await self.changed.wait()
        finally:
            sse_active_connections.dec()
            if self.finished_at is None:
                chat_stream_disconnect_total.inc()


class BackgroundChatService:
    retention_seconds = 1800
    max_jobs = 1000
    max_active_per_user = 1
    timeout_seconds = 900

    def __init__(self) -> None:
        self.jobs: dict[tuple[int, str], BackgroundChat] = {}
        self.stopped_before_start: dict[tuple[int, str], float] = {}

    def _prune(self) -> None:
        now = monotonic()
        loop = asyncio.get_running_loop()
        for key, stopped_at in list(self.stopped_before_start.items()):
            if now - stopped_at > self.retention_seconds:
                del self.stopped_before_start[key]
        for key, job in list(self.jobs.items()):
            if job.ready.get_loop() is not loop or (
                job.finished_at is not None and now - job.finished_at > self.retention_seconds
            ):
                del self.jobs[key]

    def get(self, user_id: int, request_id: str) -> BackgroundChat:
        self._prune()
        job = self.jobs.get((user_id, request_id))
        if job is None:
            raise HTTPException(404, "后台请求已结束或服务已重启，请到学习记录查看已保存的回复。")
        return job

    async def stop(self, user_id: int, request_id: str) -> BackgroundChat | None:
        self._prune()
        key = (user_id, request_id)
        job = self.jobs.get(key)
        if job is None:
            # A stop can reach the server before the multipart POST is parsed.
            if len(self.stopped_before_start) >= self.max_jobs:
                self.stopped_before_start.pop(next(iter(self.stopped_before_start)))
            self.stopped_before_start[key] = monotonic()
            return None
        if job.task and not job.task.done() and not job.task.cancelling():
            job.task.cancel()
        if job.task:
            await asyncio.shield(asyncio.gather(job.task, return_exceptions=True))
        if not job.ready.done():  # Cancelled before the producer first ran.
            job.ready.set_exception(HTTPException(409, "本次生成已停止"))
            job.finished_at = monotonic()
            job.changed.set()
        return job

    async def shutdown(self) -> None:
        tasks = [job.task for job in self.jobs.values() if job.task and not job.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def start(
        self, user_id: int, request_id: str, fingerprint: str,
        produce: Callable[[], Awaitable[StreamingResponse]],
    ) -> BackgroundChat:
        self._prune()
        key = (user_id, request_id)
        if key in self.stopped_before_start:
            raise HTTPException(409, "本次生成已停止")
        existing = self.jobs.get(key)
        if existing is not None:
            if existing.fingerprint != fingerprint:
                raise HTTPException(409, "Request id already used with different payload")
            failure = existing.ready.exception() if existing.finished_at is not None and not existing.ready.cancelled() else None
            if not (
                isinstance(failure, HTTPException)
                and isinstance(failure.detail, dict)
                and failure.detail.get("code") == "topic_classifier_unavailable"
            ):
                return existing
            del self.jobs[key]
        if sum(uid == user_id and job.finished_at is None for (uid, _), job in self.jobs.items()) >= self.max_active_per_user:
            raise HTTPException(429, "已有一个回答正在生成，请等待完成或停止后重试。")
        if len(self.jobs) >= self.max_jobs:
            completed = [key for key, job in self.jobs.items() if job.finished_at is not None]
            if not completed:
                raise HTTPException(503, "当前后台请求较多，请稍后重试。")
            del self.jobs[min(completed, key=lambda key: self.jobs[key].finished_at)]
        job = BackgroundChat(fingerprint, asyncio.get_running_loop().create_future())
        self.jobs[key] = job
        # Retrieve errors even if the client leaves before preparation finishes.
        job.ready.add_done_callback(lambda future: None if future.cancelled() else future.exception())

        async def run() -> None:
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    response = await produce()
                    job.ready.set_result(None)
                    async for frame in response.body_iterator:
                        job.frames.append(frame)
                        job.changed.set()
            except asyncio.CancelledError:
                if not job.ready.done():
                    job.ready.set_exception(HTTPException(409, "本次生成已停止"))
                else:
                    job.frames.append('event: error\ndata: {"message":"本次生成已停止"}\n\n')
            except Exception as exc:
                if not job.ready.done():
                    job.ready.set_exception(exc)
                else:
                    logger.exception("Background chat generation failed")
                    job.frames.append("event: error\ndata: " + json.dumps(
                        {"message": "模型调用失败，请联系管理员老师或稍后再试。"}, ensure_ascii=False,
                    ) + "\n\n")
            finally:
                job.finished_at = monotonic()
                job.changed.set()

        job.task = asyncio.create_task(run(), name=f"background-chat-{user_id}-{request_id}")
        return job


background_chat_service = BackgroundChatService()
