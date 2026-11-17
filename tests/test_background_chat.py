import asyncio
from io import BytesIO

from fastapi import HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
import pytest
from sqlalchemy import select
from starlette.datastructures import Headers

from backend.models.conversation import Message, MessageRole
from backend.models.schemas import ChatRequest
from backend.routers import chat
from backend.services.background_chat_service import BackgroundChatService
from backend.services.chat_image_understanding_service import ImageUnderstandingResult
from backend.services.rag_service import RetrievalResult
from backend.services.store_service import MemoryStore
from tests.test_chat_stream import (
    _build_session_factory, _create_student, _make_test_image_bytes, _parse_sse, _read_streaming_response,
)


def test_subscriber_disconnect_during_thinking_does_not_cancel_generation():
    async def scenario():
        service = BackgroundChatService()
        finish = asyncio.Event()
        calls = []

        async def produce():
            calls.append(True)
            async def frames():
                yield 'meta'
                await finish.wait()
                yield 'answer'
            return StreamingResponse(frames())

        job = service.start(1, 'request', 'same', produce)
        viewer = job.subscribe()
        assert await anext(viewer) == 'meta'
        await viewer.aclose()
        assert not job.task.done()
        # Retrying the same POST attaches to the same producer.
        assert service.start(1, 'request', 'same', produce) is job
        with pytest.raises(HTTPException) as mismatch:
            service.start(1, 'request', 'different', produce)
        assert mismatch.value.status_code == 409
        with pytest.raises(HTTPException) as stranger:
            service.get(2, 'request')
        assert stranger.value.status_code == 404
        finish.set()
        await job.task
        assert [frame async for frame in job.subscribe()] == ['meta', 'answer']
        assert len(calls) == 1
    asyncio.run(scenario())


def test_explicit_stop_and_shutdown_release_producer():
    async def scenario():
        service = BackgroundChatService()
        closed = asyncio.Event()
        async def produce():
            async def frames():
                try:
                    yield 'meta'
                    await asyncio.Event().wait()
                finally:
                    closed.set()
            return StreamingResponse(frames())
        job = service.start(1, 'request', 'same', produce)
        await asyncio.shield(job.ready)
        await service.shutdown()
        assert closed.is_set()
        assert job.finished_at is not None
    asyncio.run(scenario())


def test_stop_before_post_prevents_late_generation():
    async def scenario():
        service = BackgroundChatService()
        async def produce():
            pytest.fail('stopped request must not start')
        assert await service.stop(1, 'late-post') is None
        with pytest.raises(HTTPException) as stopped:
            service.start(1, 'late-post', 'fingerprint', produce)
        assert stopped.value.status_code == 409
    asyncio.run(scenario())


def test_disconnect_while_preparing_keeps_producer_and_ready_future_alive():
    async def scenario():
        service = BackgroundChatService()
        finish = asyncio.Event()
        async def produce():
            await finish.wait()
            async def frames():
                yield 'answer'
            return StreamingResponse(frames())
        job = service.start(1, 'preparing', 'same', produce)
        viewer = asyncio.create_task(anext(job.subscribe()))
        await asyncio.sleep(0)
        viewer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await viewer
        assert not job.ready.cancelled()
        assert not job.task.done()
        finish.set()
        await job.task
        assert [frame async for frame in job.subscribe()] == ['answer']
    asyncio.run(scenario())


def test_background_endpoint_persists_full_answer_after_request_session_closes(monkeypatch):
    factory = _build_session_factory()
    student = _create_student(factory)
    service = BackgroundChatService()
    monkeypatch.setattr(chat, 'background_chat_service', service)
    monkeypatch.setattr(chat.rag_service, 'retrieve', lambda *a, **k: RetrievalResult(context='', chunks=[]))
    monkeypatch.setattr(chat.question_cache_service, 'store_backend', MemoryStore())
    monkeypatch.setattr(chat.request_replay_service, 'store_backend', MemoryStore())
    monkeypatch.setattr(chat.question_cache_service, 'is_cacheable', lambda **k: False)
    calls = []

    async def scenario():
        finish = asyncio.Event()
        async def provider(messages, fallback_text):
            calls.append(True)
            await finish.wait()
            yield '先圈出题目中的已知条件。'
            yield '你准备选择哪个物理模型？'
        async def suggestions(**kwargs):
            return []
        async def academic_topic(**kwargs):
            return 'academic'
        monkeypatch.setattr(chat.intent_classify_service, 'classify_topic', academic_topic)
        monkeypatch.setattr(chat.llm_service, 'stream_response', provider)
        monkeypatch.setattr(chat.suggested_reply_service, 'generate', suggestions)
        payload = ChatRequest(subject='物理', message='圆周运动如何判断受力', request_id='background-test')
        async def parse(request):
            return payload, None
        monkeypatch.setattr(chat, '_parse_stream_request_payload', parse)
        request = Request({'type': 'http', 'state': {}, 'headers': []})
        session = factory()
        response = await chat.stream_chat_endpoint(request, session, student)
        assert 'event: meta' in await anext(response.body_iterator)
        await response.body_iterator.aclose()
        session.close()
        job = service.get(student.id, payload.request_id)
        assert not job.task.done()
        finish.set()
        await job.task
        with factory() as check:
            rows = check.scalars(select(Message).order_by(Message.id)).all()
            assert [row.role for row in rows] == [MessageRole.USER, MessageRole.ASSISTANT]
            assert rows[1].content == '先圈出题目中的已知条件。你准备选择哪个物理模型？'
            response = await chat.resume_chat_request(payload.request_id, request, check, student)
            events = _parse_sse(await _read_streaming_response(response))
            assert next(data['content'] for event, data in events if event == 'done') == rows[1].content
            # Completed reply can also be restored after in-memory stream retention ends.
            service.jobs.clear()
            response = await chat.resume_chat_request(payload.request_id, request, check, student)
            assert 'event: done' in await _read_streaming_response(response)
        assert len(calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('with_image', [False, True])
def test_stopped_unanswered_question_is_replaced_and_answered_in_same_turn(monkeypatch, tmp_path, with_image):
    factory = _build_session_factory()
    student = _create_student(factory)
    other_student = _create_student(factory)
    monkeypatch.setattr(chat, 'background_chat_service', BackgroundChatService())
    monkeypatch.setattr(chat.rag_service, 'retrieve', lambda *a, **k: RetrievalResult(context='', chunks=[]))
    monkeypatch.setattr(chat.request_replay_service, 'store_backend', MemoryStore())
    monkeypatch.setattr(chat.question_cache_service, 'is_cacheable', lambda **k: False)
    monkeypatch.setattr(chat.chat_attachment_service, 'base_dir', tmp_path)
    prompts = []
    image_bytes = _make_test_image_bytes() if with_image else None
    image_reads = []

    async def understand(**kwargs):
        image_reads.append(kwargs['image_bytes'])
        return ImageUnderstandingResult(
            filter_text='圆周运动的受力分析', prompt_summary='题图给出一个做圆周运动的物体。',
            ocr_raw_text='圆周运动', confidence_level='high', source='ocr', must_short_circuit=False,
        )

    monkeypatch.setattr(chat.chat_image_understanding_service, 'understand', understand)

    async def academic_topic(**kwargs):
        return 'academic'

    async def suggestions(**kwargs):
        return []

    async def scenario():
        provider_started = asyncio.Event()
        release = asyncio.Event()

        async def provider(messages, fallback_text):
            prompts.append(str(messages))
            provider_started.set()
            await release.wait()
            yield '先画出受力示意图，再判断合力的方向。'

        monkeypatch.setattr(chat.intent_classify_service, 'classify_topic', academic_topic)
        monkeypatch.setattr(chat.llm_service, 'stream_response', provider)
        monkeypatch.setattr(chat.suggested_reply_service, 'generate', suggestions)

        async def submit(db, user, payload, image=None):
            async def parse(request):
                return payload, image
            monkeypatch.setattr(chat, '_parse_stream_request_payload', parse)
            return await chat.stream_chat_endpoint(Request({'type': 'http', 'headers': []}), db, user)

        with factory() as db:
            original = ChatRequest(subject='物理', message='圆周运东怎样判断受力？', request_id='typo')
            image = UploadFile(BytesIO(image_bytes), filename='question.png', headers=Headers({'content-type': 'image/png'})) if image_bytes else None
            response = await submit(db, student, original, image)
            assert 'event: meta' in await anext(response.body_iterator)
            await provider_started.wait()
            result = await chat.stop_chat_request('typo', db, student)
            await response.body_iterator.aclose()
            assert result['editable']
            original_id = result['message_id']

        with factory() as db:
            corrected = ChatRequest(
                subject='物理', message='圆周运动怎样判断受力？', request_id='corrected',
                conversation_id=result['conversation_id'], edit_message_id=original_id,
            )
            with pytest.raises(HTTPException) as forbidden:
                await submit(db, other_student, corrected)
            assert forbidden.value.status_code == 404
            release.set()
            response = await submit(db, student, corrected)
            assert 'event: done' in await _read_streaming_response(response)
            assert not (await chat.stop_chat_request('corrected', db, student))['editable']
            with pytest.raises(HTTPException) as answered:
                await submit(db, student, corrected.model_copy(update={'request_id': 'edit-answered'}))
            assert answered.value.status_code == 409

        with factory() as db:
            rows = list(db.scalars(select(Message).order_by(Message.id)))
            assert [row.role for row in rows] == [MessageRole.USER, MessageRole.ASSISTANT]
            assert rows[0].id == original_id and rows[0].content == corrected.message
            assert rows[0].delivery_status == 'completed'
            assert rows[0].turn_index == rows[1].turn_index == 1
            assert corrected.message in prompts[-1] and original.message not in prompts[-1]
            if with_image:
                assert rows[0].attachment.original_filename == 'question.png'
                assert image_reads == [image_bytes, image_bytes]
                assert len(list(tmp_path.rglob('*.png'))) == 1

    asyncio.run(scenario())
