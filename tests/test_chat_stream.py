import asyncio
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models import agent_config, audit_log, conversation, knowledge, user  # noqa: F401
from backend.models.conversation import GuidanceStage, Message, MessageRole
from backend.models.knowledge import KnowledgeChunk, KnowledgeDocument, ResourceType
from backend.models.schemas import ChatRequest, QuestionRecommendationRequest
from backend.models.user import User, UserRole
from backend.routers import chat as chat_router
from backend.services.rag_service import RetrievalResult
from backend.services.store_service import MemoryStore


def _parse_sse(payload: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for frame in payload.strip().split("\n\n"):
        if not frame.strip():
            continue
        event_name = ""
        data: dict = {}
        for line in frame.splitlines():
            if line.startswith("event:"):
                event_name = line.replace("event:", "", 1).strip()
            if line.startswith("data:"):
                data = json.loads(line.replace("data:", "", 1).strip())
        if event_name:
            events.append((event_name, data))
    return events


def _build_session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    return TestingSessionLocal


class FakeRequest:
    def __init__(self) -> None:
        self.state = SimpleNamespace(request_id="test-request")

    async def is_disconnected(self) -> bool:
        return False


def _create_user(session_factory, *, role: UserRole, grade: int | None = None) -> User:
    session = session_factory()
    try:
        user = User(
            username=f"{role.value}1",
            student_no="20260001" if role == UserRole.STUDENT else None,
            full_name="测试用户",
            role=role,
            password_hash="fake-hash",
            must_change_password=False,
            is_active=True,
            grade=grade,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        session.expunge(user)
        return user
    finally:
        session.close()


def _create_student(session_factory) -> User:
    return _create_user(session_factory, role=UserRole.STUDENT)


async def _read_streaming_response(response) -> str:
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
    return "".join(chunks)


def test_chat_stream_emits_real_chunks_and_persists(monkeypatch):
    session_factory = _build_session_factory()
    current_user = _create_student(session_factory)

    async def fake_stream_response(messages, fallback_text) -> AsyncIterator[str]:
        for chunk in ["先看定义域。", "再判断增减性。"]:
            yield chunk

    monkeypatch.setattr(chat_router.llm_service, "stream_response", fake_stream_response)
    monkeypatch.setattr(
        chat_router.rag_service,
        "retrieve",
        lambda db, subject, question, **kwargs: RetrievalResult(context="资料片段", chunks=[]),
    )

    session = session_factory()

    try:
        response = asyncio.run(
            chat_router.stream_chat(
                ChatRequest(subject="数学", message="函数单调性第一步怎么想"),
                session,
                current_user,
                FakeRequest(),
            )
        )
        events = _parse_sse(asyncio.run(_read_streaming_response(response)))
        assert [event for event, _ in events] == ["meta", "chunk", "chunk", "done"]
        assert events[-1][1]["content"] == "先看定义域。再判断增减性。"

        stored_messages = session.scalars(select(Message).order_by(Message.id.asc())).all()
        assert len(stored_messages) == 2
        assert stored_messages[0].role == MessageRole.USER
        assert stored_messages[1].role == MessageRole.ASSISTANT
        assert stored_messages[1].content == "先看定义域。再判断增减性。"
    finally:
        session.close()


def test_chat_stream_rewrites_unsafe_output_before_emitting(monkeypatch):
    session_factory = _build_session_factory()
    current_user = _create_student(session_factory)

    async def fake_stream_response(messages, fallback_text) -> AsyncIterator[str]:
        yield "最终答案是 A"

    monkeypatch.setattr(chat_router.llm_service, "stream_response", fake_stream_response)
    monkeypatch.setattr(
        chat_router.rag_service,
        "retrieve",
        lambda db, subject, question, **kwargs: RetrievalResult(context="", chunks=[]),
    )

    session = session_factory()

    try:
        response = asyncio.run(
            chat_router.stream_chat(
                ChatRequest(subject="数学", message="这题选什么"),
                session,
                current_user,
                FakeRequest(),
            )
        )
        events = _parse_sse(asyncio.run(_read_streaming_response(response)))
        assert [event for event, _ in events] == ["meta", "chunk", "done"]
        final_text = events[-1][1]["content"]
        assert "最终答案是 A" not in final_text
        assert final_text == chat_router.socratic_service.safe_guided_rewrite("这题选什么", "数学", GuidanceStage.INITIAL)
    finally:
        session.close()


def test_chat_stream_reuses_hot_question_cache(monkeypatch):
    session_factory = _build_session_factory()
    current_user = _create_student(session_factory)
    llm_call_count = {"value": 0}

    async def fake_stream_response(messages, fallback_text) -> AsyncIterator[str]:
        llm_call_count["value"] += 1
        yield "先看定义域。"

    monkeypatch.setattr(chat_router.llm_service, "stream_response", fake_stream_response)
    monkeypatch.setattr(
        chat_router.rag_service,
        "retrieve",
        lambda db, subject, question, **kwargs: RetrievalResult(context="", chunks=[]),
    )
    monkeypatch.setattr(chat_router.question_cache_service, "store_backend", MemoryStore())

    session = session_factory()

    try:
        first_response = asyncio.run(
            chat_router.stream_chat(
                ChatRequest(subject="数学", message="函数单调性第一步怎么想"),
                session,
                current_user,
                FakeRequest(),
            )
        )
        _ = _parse_sse(asyncio.run(_read_streaming_response(first_response)))

        second_response = asyncio.run(
            chat_router.stream_chat(
                ChatRequest(subject="数学", message="函数单调性第一步怎么想"),
                session,
                current_user,
                FakeRequest(),
            )
        )
        second_events = _parse_sse(asyncio.run(_read_streaming_response(second_response)))

        assert llm_call_count["value"] == 1
        assert [event for event, _ in second_events] == ["meta", "done"]
        assert second_events[-1][1]["content"] == "先看定义域。"
    finally:
        session.close()


def test_chat_stream_allows_students_marked_for_password_change(monkeypatch):
    session_factory = _build_session_factory()
    current_user = _create_student(session_factory)
    current_user.must_change_password = True

    async def fake_stream_response(messages, fallback_text) -> AsyncIterator[str]:
        yield "先看磁场方向。"

    monkeypatch.setattr(chat_router.llm_service, "stream_response", fake_stream_response)
    monkeypatch.setattr(
        chat_router.rag_service,
        "retrieve",
        lambda db, subject, question, **kwargs: RetrievalResult(context="", chunks=[]),
    )

    session = session_factory()

    try:
        response = asyncio.run(
            chat_router.stream_chat(
                ChatRequest(subject="物理", message="回旋加速器是什么"),
                session,
                current_user,
                FakeRequest(),
            )
        )
        events = _parse_sse(asyncio.run(_read_streaming_response(response)))
        assert [event for event, _ in events] == ["meta", "chunk", "done"]
        assert events[-1][1]["content"] == "先看磁场方向。"
    finally:
        session.close()


def test_chat_stream_replays_completed_request_id(monkeypatch):
    session_factory = _build_session_factory()
    current_user = _create_student(session_factory)
    llm_call_count = {"value": 0}

    async def fake_stream_response(messages, fallback_text) -> AsyncIterator[str]:
        llm_call_count["value"] += 1
        yield "先判断已知条件。"

    monkeypatch.setattr(chat_router.llm_service, "stream_response", fake_stream_response)
    monkeypatch.setattr(
        chat_router.rag_service,
        "retrieve",
        lambda db, subject, question, **kwargs: RetrievalResult(context="", chunks=[]),
    )
    monkeypatch.setattr(chat_router.question_cache_service, "store_backend", MemoryStore())
    monkeypatch.setattr(chat_router.request_replay_service, "store_backend", MemoryStore())
    monkeypatch.setattr(chat_router.question_cache_service, "is_cacheable", lambda **kwargs: False)

    session = session_factory()

    try:
        first_response = asyncio.run(
            chat_router.stream_chat(
                ChatRequest(subject="数学", message="函数题怎么下手", request_id="req-1"),
                session,
                current_user,
                FakeRequest(),
            )
        )
        first_events = _parse_sse(asyncio.run(_read_streaming_response(first_response)))

        second_response = asyncio.run(
            chat_router.stream_chat(
                ChatRequest(subject="数学", message="函数题怎么下手", request_id="req-1"),
                session,
                current_user,
                FakeRequest(),
            )
        )
        second_events = _parse_sse(asyncio.run(_read_streaming_response(second_response)))
        stored_messages = session.scalars(select(Message).order_by(Message.id.asc())).all()

        assert llm_call_count["value"] == 1
        assert [event for event, _ in first_events] == ["meta", "chunk", "done"]
        assert [event for event, _ in second_events] == ["meta", "done"]
        assert len(stored_messages) == 2
        assert stored_messages[1].content == "先判断已知条件。"
    finally:
        session.close()


def test_recommend_questions_returns_assets_and_hides_solutions_for_student(monkeypatch):
    session_factory = _build_session_factory()
    current_user = _create_user(session_factory, role=UserRole.STUDENT, grade=2)
    session = session_factory()
    try:
        document = KnowledgeDocument(
            subject="物理",
            filename="questions.docx",
            file_path="/tmp/questions.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=128,
            resource_type=ResourceType.QUESTION_SET.value,
            grade=2,
        )
        session.add(document)
        session.commit()
        session.refresh(document)

        row = KnowledgeChunk(
            document_id=document.id,
            subject="物理",
            chunk_index=0,
            content="第1题\n\n题目：如图所示，分析受力。",
            metadata_json={
                "document_id": document.id,
                "resource_type": document.resource_type,
                "grade": 2,
                "chunk_kind": "question_item",
                "question_number": "1",
                "question_text": "如图所示，分析受力。",
                "answer_text": "A",
                "explanation_text": "由牛顿第二定律分析。",
                "contains_images": True,
                "image_count": 1,
                "asset_refs": [
                    {
                        "asset_id": "image-001",
                        "filename": "image-001.png",
                        "content_type": "image/png",
                        "url": "/api/knowledge/documents/1/assets/image-001.png",
                        "title": "受力图",
                        "description": None,
                    }
                ],
            },
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        monkeypatch.setattr(chat_router.rag_service, "recommend_questions", lambda *args, **kwargs: [row])

        result = chat_router.recommend_questions(
            QuestionRecommendationRequest(
                subject="物理",
                question="如图所示这类受力分析题再给我一题",
                include_solutions=True,
            ),
            session,
            current_user,
        )

        assert len(result) == 1
        assert result[0].question_text == "如图所示，分析受力。"
        assert result[0].contains_images is True
        assert result[0].assets[0].filename == "image-001.png"
        assert result[0].answer_text is None
        assert result[0].explanation_text is None
    finally:
        session.close()


def test_recommend_questions_allows_students_marked_for_password_change(monkeypatch):
    session_factory = _build_session_factory()
    current_user = _create_user(session_factory, role=UserRole.STUDENT, grade=2)
    current_user.must_change_password = True
    session = session_factory()
    try:
        document = KnowledgeDocument(
            subject="物理",
            filename="questions.docx",
            file_path="/tmp/questions.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=128,
            resource_type=ResourceType.QUESTION_SET.value,
            grade=2,
        )
        session.add(document)
        session.commit()
        session.refresh(document)

        row = KnowledgeChunk(
            document_id=document.id,
            subject="物理",
            chunk_index=0,
            content="第1题\n\n题目：回旋加速器中的粒子运动。",
            metadata_json={
                "document_id": document.id,
                "resource_type": document.resource_type,
                "grade": 2,
                "chunk_kind": "question_item",
                "question_number": "1",
                "question_text": "回旋加速器中的粒子运动。",
                "contains_images": False,
                "image_count": 0,
            },
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        monkeypatch.setattr(chat_router.rag_service, "recommend_questions", lambda *args, **kwargs: [row])

        result = chat_router.recommend_questions(
            QuestionRecommendationRequest(
                subject="物理",
                question="回旋加速器相关题还有吗",
            ),
            session,
            current_user,
        )

        assert len(result) == 1
        assert result[0].question_text == "回旋加速器中的粒子运动。"
    finally:
        session.close()


def test_recommend_questions_can_include_solutions_for_teacher(monkeypatch):
    session_factory = _build_session_factory()
    current_user = _create_user(session_factory, role=UserRole.TEACHER)
    session = session_factory()
    try:
        document = KnowledgeDocument(
            subject="数学",
            filename="questions.docx",
            file_path="/tmp/questions.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=128,
            resource_type=ResourceType.EXERCISE.value,
        )
        session.add(document)
        session.commit()
        session.refresh(document)

        row = KnowledgeChunk(
            document_id=document.id,
            subject="数学",
            chunk_index=0,
            content="第2题\n\n题目：函数最值",
            metadata_json={
                "document_id": document.id,
                "resource_type": document.resource_type,
                "chunk_kind": "question_item",
                "question_number": "2",
                "question_text": "函数最值",
                "answer_text": "先配方",
                "explanation_text": "利用二次函数顶点求最值。",
                "contains_images": False,
                "image_count": 0,
            },
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        monkeypatch.setattr(chat_router.rag_service, "recommend_questions", lambda *args, **kwargs: [row])

        result = chat_router.recommend_questions(
            QuestionRecommendationRequest(
                subject="数学",
                question="给我推荐一道函数最值题",
                include_solutions=True,
                student_grade=2,
            ),
            session,
            current_user,
        )

        assert len(result) == 1
        assert result[0].answer_text == "先配方"
        assert result[0].explanation_text == "利用二次函数顶点求最值。"
    finally:
        session.close()
