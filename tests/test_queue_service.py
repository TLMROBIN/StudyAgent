import asyncio

from backend.services.queue_service import QueueFullError, QueueService


def _service(max_concurrent: int, max_waiting: int) -> QueueService:
    service = QueueService()
    service._max_waiting = max_waiting
    service._semaphore = asyncio.Semaphore(max_concurrent)
    return service


def test_cancelled_waiter_does_not_leak_slot_or_waiting_count():
    async def scenario():
        service = _service(max_concurrent=1, max_waiting=10)
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()

        async def holder():
            async with service.reserve():
                holder_entered.set()
                await release_holder.wait()

        async def waiter():
            async with service.reserve():
                pass

        holder_task = asyncio.create_task(holder())
        await holder_entered.wait()
        waiter_task = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        assert service.waiting == 1

        waiter_task.cancel()
        try:
            await waiter_task
        except asyncio.CancelledError:
            pass

        assert service.waiting == 0
        # 槽位仍被 holder 占着，取消不能凭空多放出一个并发名额
        assert service._semaphore._value == 0

        release_holder.set()
        await holder_task
        assert service._semaphore._value == 1

    asyncio.run(scenario())


def test_queue_rejects_when_waiting_limit_reached():
    async def scenario():
        service = _service(max_concurrent=1, max_waiting=1)
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()

        async def holder():
            async with service.reserve():
                holder_entered.set()
                await release_holder.wait()

        holder_task = asyncio.create_task(holder())
        await holder_entered.wait()
        try:
            async with service.reserve():
                raise AssertionError("should not acquire")
        except QueueFullError:
            pass
        release_holder.set()
        await holder_task

    asyncio.run(scenario())
