import asyncio
import json
from collections.abc import AsyncIterator
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi import UploadFile
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers

from backend.config import Settings
from backend.database import Base, get_db
from backend.dependencies import get_current_user
from backend.models import agent_config, audit_log, conversation, knowledge, user  # noqa: F401
from pydantic import ValidationError

from backend.models.audit_log import AuditLog
from backend.models.conversation import ChatMessageAttachment, Conversation, GuidanceStage, Message, MessageRole
from backend.models.knowledge import KnowledgeChunk, KnowledgeDocument, ResourceType
from backend.models.schemas import ChatRequest, QuestionRecommendationRequest
from backend.models.user import User, UserRole
from backend.routers import chat as chat_router
from backend.services.chat_image_understanding_service import ImageUnderstandingResult
from backend.services.embed_service import EmbedService
from backend.services.rag_service import RagService, RetrievalResult
from backend.services.store_service import MemoryStore
from backend.services.vector_store_service import VectorStoreService


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


def _build_rag_service(tmp_path: Path) -> RagService:
    settings = Settings(
        CHROMADB_MODE="persistent",
        CHROMADB_PATH=str(tmp_path / "chromadb"),
        CHROMADB_COLLECTION_PREFIX="studyagent-chat-test",
        TASK_ARTIFACT_PATH=str(tmp_path / "tasks"),
        UPLOAD_PATH=str(tmp_path / "uploads"),
        EMBEDDING_MODEL_NAME="BAAI/bge-m3",
        EMBEDDING_BACKEND="hash",
        EMBEDDING_DEVICE="cpu",
        EMBEDDING_FALLBACK_TO_HASH=True,
    )
    embedder = EmbedService(settings)
    vector_store = VectorStoreService(settings, embedder)
    return RagService(settings=settings, embedder=embedder, vector_store=vector_store)


def _make_test_image_bytes(color: str = "white") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (8, 8), color=color).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeRequest:
    def __init__(self) -> None:
        self.state = SimpleNamespace(request_id="test-request")

    async def is_disconnected(self) -> bool:
        return False


def _create_user(session_factory, *, role: UserRole, grade: int | None = None) -> User:
    session = session_factory()
    try:
        next_id = (session.query(user.User).count() or 0) + 1
        created_user = User(
            username=f"{role.value}{next_id}",
            student_no=f"2026{next_id:04d}" if role == UserRole.STUDENT else None,
            full_name="测试用户",
            role=role,
            password_hash="fake-hash",
            must_change_password=False,
            is_active=True,
            grade=grade,
        )
        session.add(created_user)
        session.commit()
        session.refresh(created_user)
        session.expunge(created_user)
        return created_user
    finally:
        session.close()


def _create_student(session_factory) -> User:
    return _create_user(session_factory, role=UserRole.STUDENT)


def _build_chat_test_client(session_factory, current_user: User):
    app = FastAPI()
    app.include_router(chat_router.router)

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    def override_current_user():
        return current_user

    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


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
    monkeypatch.setattr(chat_router.question_cache_service, "is_cacheable", lambda **kwargs: False)

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


def test_student_can_list_builtin_chat_models():
    session_factory = _build_session_factory()
    client = _build_chat_test_client(session_factory, _create_student(session_factory))

    response = client.get("/api/chat/models")

    assert response.status_code == 200
    assert response.json() == [
        {"key": "minimax-m27", "name": "MiniMax-M2.7", "description": "highspeed"},
        {"key": "qwen2.5-vl", "name": "qwen2.5-vl", "description": "图片理解推荐使用，但响应速度可能较慢。"},
    ]


def test_student_can_list_chat_model_statuses(monkeypatch):
    session_factory = _build_session_factory()
    client = _build_chat_test_client(session_factory, _create_student(session_factory))

    async def fake_statuses(*, force_refresh=False):
        return [
            {"key": "minimax-m27", "status": "available", "message": ""},
            {"key": "qwen2.5-vl", "status": "unavailable", "message": "连接失败"},
        ]

    monkeypatch.setattr(chat_router.llm_service, "chat_model_statuses", fake_statuses)

    response = client.get("/api/chat/models/status")

    assert response.status_code == 200
    assert response.json() == [
        {"key": "minimax-m27", "status": "available", "message": ""},
        {"key": "qwen2.5-vl", "status": "unavailable", "message": "连接失败"},
    ]


def test_chat_stream_forwards_selected_model_to_llm_service(monkeypatch):
    session_factory = _build_session_factory()
    current_user = _create_student(session_factory)
    selected_models: list[str | None] = []

    async def fake_stream_response(messages, fallback_text, *, model_key=None) -> AsyncIterator[str]:
        selected_models.append(model_key)
        yield "先读图中条件。"

    monkeypatch.setattr(chat_router.llm_service, "stream_response", fake_stream_response)
    monkeypatch.setattr(
        chat_router.rag_service,
        "retrieve",
        lambda db, subject, question, **kwargs: RetrievalResult(context="资料片段", chunks=[]),
    )
    monkeypatch.setattr(chat_router.question_cache_service, "is_cacheable", lambda **kwargs: False)

    session = session_factory()
    try:
        response = asyncio.run(
            chat_router.stream_chat(
                ChatRequest(subject="物理", message="这张图怎么分析", llm_model="qwen2.5-vl"),
                session,
                current_user,
                FakeRequest(),
            )
        )
        events = _parse_sse(asyncio.run(_read_streaming_response(response)))
        assistant_message = session.scalar(
            select(Message).where(Message.role == MessageRole.ASSISTANT).order_by(Message.id.desc()).limit(1)
        )
    finally:
        session.close()

    assert selected_models == ["qwen2.5-vl"]
    assert events[-1][1]["content"] == "先读图中条件。"
    assert assistant_message is not None
    assert assistant_message.llm_model_key == "qwen2.5-vl"


def test_chat_stream_replaces_empty_llm_stream_with_student_fallback(monkeypatch):
    session_factory = _build_session_factory()
    current_user = _create_student(session_factory)

    async def fake_stream_response(messages, fallback_text) -> AsyncIterator[str]:
        if False:
            yield ""

    monkeypatch.setattr(chat_router.llm_service, "stream_response", fake_stream_response)
    monkeypatch.setattr(
        chat_router.rag_service,
        "retrieve",
        lambda db, subject, question, **kwargs: RetrievalResult(context="", chunks=[]),
    )
    monkeypatch.setattr(chat_router.question_cache_service, "is_cacheable", lambda **kwargs: False)

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
        assert [event for event, _ in events] == ["meta", "done"]
        assert events[-1][1]["content"] == chat_router.EMPTY_CHAT_RESPONSE_FALLBACK

        stored_messages = session.scalars(select(Message).order_by(Message.id.asc())).all()
        assert len(stored_messages) == 2
        assert stored_messages[1].role == MessageRole.ASSISTANT
        assert stored_messages[1].content == chat_router.EMPTY_CHAT_RESPONSE_FALLBACK
    finally:
        session.close()


def test_chat_stream_empty_image_response_uses_guided_fallback(monkeypatch):
    session_factory = _build_session_factory()
    current_user = _create_student(session_factory)

    async def fake_stream_response(messages, fallback_text, *, model_key=None) -> AsyncIterator[str]:
        if False:
            yield ""

    async def fake_understand(**kwargs):
        return ImageUnderstandingResult(
            filter_text="如图，已知函数图像经过点 A，求单调区间。",
            prompt_summary="如图，已知函数图像经过点 A，求单调区间。",
            ocr_raw_text="如图 已知函数图像经过点 A 求单调区间",
            confidence_level="high",
            source="ocr",
            must_short_circuit=False,
        )

    monkeypatch.setattr(chat_router.llm_service, "stream_response", fake_stream_response)
    monkeypatch.setattr(chat_router.chat_image_understanding_service, "understand", fake_understand)
    monkeypatch.setattr(
        chat_router.rag_service,
        "retrieve",
        lambda db, subject, question, **kwargs: RetrievalResult(context="", chunks=[]),
    )
    monkeypatch.setattr(chat_router.question_cache_service, "is_cacheable", lambda **kwargs: False)

    client = _build_chat_test_client(session_factory, current_user)
    response = client.post(
        "/api/chat/stream",
        data={"subject": "数学", "message": ""},
        files={"image": ("question.png", _make_test_image_bytes(), "image/png")},
    )

    assert response.status_code == 200
    events = _parse_sse(response.text)
    final_text = events[-1][1]["content"]
    assert [event for event, _ in events] == ["meta", "done"]
    assert final_text != chat_router.filter_service.image_uncertainty_text
    assert "我识别到这张数学题图里主要有" in final_text
    assert "函数图像经过点 A" in final_text
    assert "先不要急着算结果" in final_text
    assert "我是 AI" in final_text


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
    monkeypatch.setattr(chat_router.question_cache_service, "is_cacheable", lambda **kwargs: False)

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
    monkeypatch.setattr(chat_router.question_cache_service, "is_cacheable", lambda **kwargs: False)

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


def test_chat_stream_allows_admin_user_via_http(monkeypatch):
    session_factory = _build_session_factory()
    current_user = _create_user(session_factory, role=UserRole.ADMIN)

    async def fake_stream_response(messages, fallback_text) -> AsyncIterator[str]:
        yield "先从题干条件入手。"

    monkeypatch.setattr(chat_router.llm_service, "stream_response", fake_stream_response)
    monkeypatch.setattr(
        chat_router.rag_service,
        "retrieve",
        lambda db, subject, question, **kwargs: RetrievalResult(context="", chunks=[]),
    )
    monkeypatch.setattr(chat_router.question_cache_service, "is_cacheable", lambda **kwargs: False)

    client = _build_chat_test_client(session_factory, current_user)
    response = client.post("/api/chat/stream", json={"subject": "数学", "message": "管理员试问一道题"})

    assert response.status_code == 200
    assert "Insufficient permissions" not in response.text
    assert "先从题干条件入手。" in response.text


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


def test_chat_stream_supports_image_only_messages_and_persists_attachment(monkeypatch):
    session_factory = _build_session_factory()
    current_user = _create_student(session_factory)

    async def fake_stream_response(messages, fallback_text) -> AsyncIterator[str]:
        yield "先看看图里标出的已知条件。"

    async def fake_understand(**kwargs):
        return ImageUnderstandingResult(
            filter_text="已知函数图像和坐标点",
            prompt_summary="题目给了一张函数图像，并标出了一个坐标点。",
            ocr_raw_text="函数图像 坐标点",
            confidence_level="high",
            source="ocr",
            must_short_circuit=False,
        )

    monkeypatch.setattr(chat_router.llm_service, "stream_response", fake_stream_response)
    monkeypatch.setattr(chat_router.chat_image_understanding_service, "understand", fake_understand)
    monkeypatch.setattr(
        chat_router.rag_service,
        "retrieve",
        lambda db, subject, question, **kwargs: RetrievalResult(context="", chunks=[]),
    )
    monkeypatch.setattr(chat_router.question_cache_service, "is_cacheable", lambda **kwargs: False)

    client = _build_chat_test_client(session_factory, current_user)
    response = client.post(
        "/api/chat/stream",
        data={"subject": "数学", "message": "", "request_id": "image-only-1"},
        files={"image": ("question.png", _make_test_image_bytes(), "image/png")},
    )

    assert response.status_code == 200
    events = _parse_sse(response.text)
    assert [event for event, _ in events] == ["meta", "chunk", "done"]
    assert "我是 AI" in events[-1][1]["content"]
    assert "一起讨论探索" in events[-1][1]["content"]

    session = session_factory()
    try:
        stored_messages = session.scalars(select(Message).order_by(Message.id.asc())).all()
        attachments = session.scalars(select(ChatMessageAttachment).order_by(ChatMessageAttachment.id.asc())).all()
        assert len(stored_messages) == 2
        assert stored_messages[0].content == "[图片提问]"
        assert len(attachments) == 1
        assert attachments[0].original_filename == "question.png"
        assert attachments[0].ocr_status == "llm_ocr"
    finally:
        session.close()


def test_chat_stream_records_mineru_ocr_status_for_image_turn(monkeypatch):
    session_factory = _build_session_factory()
    current_user = _create_student(session_factory)

    async def fake_stream_response(messages, fallback_text) -> AsyncIterator[str]:
        yield "先看 OCR 提取出的已知条件。"

    async def fake_understand(**kwargs):
        assert kwargs["image_path"]
        assert kwargs["attachment_id"]
        return ImageUnderstandingResult(
            filter_text="已知函数 f(x)=x^2，求单调区间",
            prompt_summary="已知函数 f(x)=x^2，求单调区间",
            ocr_raw_text="已知函数 f(x)=x^2，求单调区间",
            confidence_level="high",
            source="mineru_ocr",
            must_short_circuit=False,
        )

    monkeypatch.setattr(chat_router.llm_service, "stream_response", fake_stream_response)
    monkeypatch.setattr(chat_router.chat_image_understanding_service, "understand", fake_understand)
    monkeypatch.setattr(
        chat_router.rag_service,
        "retrieve",
        lambda db, subject, question, **kwargs: RetrievalResult(context="", chunks=[]),
    )
    monkeypatch.setattr(chat_router.question_cache_service, "is_cacheable", lambda **kwargs: False)

    client = _build_chat_test_client(session_factory, current_user)
    response = client.post(
        "/api/chat/stream",
        data={"subject": "数学", "message": "", "request_id": "mineru-image-1"},
        files={"image": ("question.png", _make_test_image_bytes(), "image/png")},
    )

    assert response.status_code == 200
    session = session_factory()
    try:
        attachment = session.scalar(select(ChatMessageAttachment))
        assert attachment is not None
        assert attachment.ocr_status == "mineru_ocr"
    finally:
        session.close()


def test_chat_stream_records_paddleocr_status_for_image_turn(monkeypatch):
    session_factory = _build_session_factory()
    current_user = _create_student(session_factory)

    async def fake_stream_response(messages, fallback_text, *, model_key=None) -> AsyncIterator[str]:
        yield "先看 OCR 提取出的题干。"

    async def fake_understand(**kwargs):
        return ImageUnderstandingResult(
            filter_text="如图，空间存在水平向左的匀强电场。",
            prompt_summary="如图，空间存在水平向左的匀强电场。",
            ocr_raw_text="如图 空间存在水平向左的匀强电场",
            confidence_level="high",
            source="paddleocr",
            must_short_circuit=False,
        )

    monkeypatch.setattr(chat_router.llm_service, "stream_response", fake_stream_response)
    monkeypatch.setattr(chat_router.chat_image_understanding_service, "understand", fake_understand)
    monkeypatch.setattr(
        chat_router.rag_service,
        "retrieve",
        lambda db, subject, question, **kwargs: RetrievalResult(context="", chunks=[]),
    )
    monkeypatch.setattr(chat_router.question_cache_service, "is_cacheable", lambda **kwargs: False)

    client = _build_chat_test_client(session_factory, current_user)
    response = client.post(
        "/api/chat/stream",
        data={"subject": "物理", "message": ""},
        files={"image": ("question.png", _make_test_image_bytes(), "image/png")},
    )

    assert response.status_code == 200
    session = session_factory()
    try:
        attachment = session.scalar(select(ChatMessageAttachment))
        assert attachment is not None
        assert attachment.ocr_status == "paddleocr"
    finally:
        session.close()


def test_chat_stream_passes_selected_model_to_image_understanding(monkeypatch):
    session_factory = _build_session_factory()
    current_user = _create_student(session_factory)
    seen: dict[str, object] = {}

    async def fake_stream_response(messages, fallback_text, *, model_key=None) -> AsyncIterator[str]:
        yield "先看图中标出的条件。"

    async def fake_understand(**kwargs):
        seen["model_key"] = kwargs["model_key"]
        return ImageUnderstandingResult(
            filter_text="已知函数图像和坐标点",
            prompt_summary="题目给了一张函数图像，并标出了一个坐标点。",
            ocr_raw_text="函数图像 坐标点",
            confidence_level="high",
            source="ocr",
            must_short_circuit=False,
        )

    monkeypatch.setattr(chat_router.llm_service, "stream_response", fake_stream_response)
    monkeypatch.setattr(chat_router.chat_image_understanding_service, "understand", fake_understand)
    monkeypatch.setattr(
        chat_router.rag_service,
        "retrieve",
        lambda db, subject, question, **kwargs: RetrievalResult(context="", chunks=[]),
    )
    monkeypatch.setattr(chat_router.question_cache_service, "is_cacheable", lambda **kwargs: False)

    client = _build_chat_test_client(session_factory, current_user)
    response = client.post(
        "/api/chat/stream",
        data={"subject": "数学", "message": "", "llm_model": "minimax-m27"},
        files={"image": ("question.png", _make_test_image_bytes(), "image/png")},
    )

    assert response.status_code == 200
    assert seen["model_key"] == "minimax-m27"


def test_chat_stream_short_circuits_low_confidence_images(monkeypatch):
    session_factory = _build_session_factory()
    current_user = _create_student(session_factory)
    llm_called = {"value": 0}

    async def fake_stream_response(messages, fallback_text) -> AsyncIterator[str]:
        llm_called["value"] += 1
        yield "不应该被调用"

    async def fake_understand(**kwargs):
        return ImageUnderstandingResult(
            filter_text="",
            prompt_summary="",
            ocr_raw_text="模糊",
            confidence_level="low",
            source="failed",
            must_short_circuit=True,
        )

    monkeypatch.setattr(chat_router.llm_service, "stream_response", fake_stream_response)
    monkeypatch.setattr(chat_router.chat_image_understanding_service, "understand", fake_understand)
    monkeypatch.setattr(chat_router.question_cache_service, "is_cacheable", lambda **kwargs: False)

    client = _build_chat_test_client(session_factory, current_user)
    response = client.post(
        "/api/chat/stream",
        data={"subject": "物理", "message": ""},
        files={"image": ("blur.png", _make_test_image_bytes("gray"), "image/png")},
    )

    assert response.status_code == 200
    events = _parse_sse(response.text)
    assert [event for event, _ in events] == ["meta", "done"]
    assert "可能会看错图片" in events[-1][1]["content"]
    assert "重拍" in events[-1][1]["content"]
    assert llm_called["value"] == 0


def test_chat_history_includes_attachment_payload_for_image_turn(monkeypatch):
    session_factory = _build_session_factory()
    current_user = _create_student(session_factory)

    async def fake_stream_response(messages, fallback_text) -> AsyncIterator[str]:
        yield "先圈出图中的已知条件。"

    async def fake_understand(**kwargs):
        return ImageUnderstandingResult(
            filter_text="已知受力图和方向",
            prompt_summary="图片里给出了受力图和方向标注。",
            ocr_raw_text="受力图 方向",
            confidence_level="high",
            source="ocr",
            must_short_circuit=False,
        )

    monkeypatch.setattr(chat_router.llm_service, "stream_response", fake_stream_response)
    monkeypatch.setattr(chat_router.chat_image_understanding_service, "understand", fake_understand)
    monkeypatch.setattr(
        chat_router.rag_service,
        "retrieve",
        lambda db, subject, question, **kwargs: RetrievalResult(context="", chunks=[]),
    )
    monkeypatch.setattr(chat_router.question_cache_service, "is_cacheable", lambda **kwargs: False)

    client = _build_chat_test_client(session_factory, current_user)
    stream_response = client.post(
        "/api/chat/stream",
        data={"subject": "物理", "message": ""},
        files={"image": ("force.png", _make_test_image_bytes("blue"), "image/png")},
    )
    assert stream_response.status_code == 200

    history = client.get("/api/chat/history")
    assert history.status_code == 200
    conversation_id = history.json()[0]["id"]

    detail = client.get(f"/api/chat/history/{conversation_id}")
    assert detail.status_code == 200
    messages = detail.json()["messages"]
    assert messages[0]["attachment"]["filename"] == "force.png"
    assert messages[0]["attachment"]["url"].startswith("/api/chat/attachments/")


def test_student_can_delete_own_conversation(monkeypatch):
    session_factory = _build_session_factory()
    current_user = _create_student(session_factory)

    async def fake_stream_response(messages, fallback_text) -> AsyncIterator[str]:
        yield "先看题干条件。"

    monkeypatch.setattr(chat_router.llm_service, "stream_response", fake_stream_response)
    monkeypatch.setattr(
        chat_router.rag_service,
        "retrieve",
        lambda db, subject, question, **kwargs: RetrievalResult(context="", chunks=[]),
    )
    monkeypatch.setattr(chat_router.question_cache_service, "is_cacheable", lambda **kwargs: False)

    client = _build_chat_test_client(session_factory, current_user)
    stream_response = client.post("/api/chat/stream", json={"subject": "数学", "message": "函数题怎么想"})
    assert stream_response.status_code == 200

    conversation_id = client.get("/api/chat/history").json()[0]["id"]
    delete_response = client.delete(f"/api/chat/{conversation_id}")
    assert delete_response.status_code == 200
    assert delete_response.json() == {"status": "deleted"}
    assert client.get("/api/chat/history").json() == []

    session = session_factory()
    try:
        retained_conversation = session.get(Conversation, conversation_id)
        assert retained_conversation is not None
        assert retained_conversation.deleted_by_student_at is not None
        retained_messages = session.scalars(
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id.asc())
        ).all()
        assert [message.role for message in retained_messages] == [MessageRole.USER, MessageRole.ASSISTANT]
        audit_entry = session.scalar(select(AuditLog).where(AuditLog.action == "student_clear_conversation"))
        assert audit_entry is not None
        assert audit_entry.target_type == "conversation"
        assert audit_entry.target_id == str(conversation_id)
    finally:
        session.close()


def test_student_cannot_delete_another_students_conversation():
    session_factory = _build_session_factory()
    owner = _create_student(session_factory)
    intruder = _create_user(session_factory, role=UserRole.STUDENT)

    session = session_factory()
    try:
        conversation_row = Conversation(student_id=owner.id, subject="数学")
        session.add(conversation_row)
        session.commit()
        session.refresh(conversation_row)
        conversation_id = conversation_row.id
    finally:
        session.close()

    intruder_client = _build_chat_test_client(session_factory, intruder)
    assert intruder_client.delete(f"/api/chat/{conversation_id}").status_code == 404

    owner_client = _build_chat_test_client(session_factory, owner)
    assert owner_client.get("/api/chat/history").json()[0]["id"] == conversation_id


def test_chat_attachment_route_is_private_to_owner(monkeypatch):
    session_factory = _build_session_factory()
    owner = _create_student(session_factory)
    intruder = _create_user(session_factory, role=UserRole.STUDENT)

    async def fake_stream_response(messages, fallback_text) -> AsyncIterator[str]:
        yield "先看图中的坐标。"

    async def fake_understand(**kwargs):
        return ImageUnderstandingResult(
            filter_text="坐标系 图像",
            prompt_summary="图片中有坐标系和函数图像。",
            ocr_raw_text="坐标系 图像",
            confidence_level="high",
            source="ocr",
            must_short_circuit=False,
        )

    monkeypatch.setattr(chat_router.llm_service, "stream_response", fake_stream_response)
    monkeypatch.setattr(chat_router.chat_image_understanding_service, "understand", fake_understand)
    monkeypatch.setattr(
        chat_router.rag_service,
        "retrieve",
        lambda db, subject, question, **kwargs: RetrievalResult(context="", chunks=[]),
    )
    monkeypatch.setattr(chat_router.question_cache_service, "is_cacheable", lambda **kwargs: False)

    owner_client = _build_chat_test_client(session_factory, owner)
    response = owner_client.post(
        "/api/chat/stream",
        data={"subject": "数学", "message": ""},
        files={"image": ("graph.png", _make_test_image_bytes("green"), "image/png")},
    )
    assert response.status_code == 200

    history = owner_client.get("/api/chat/history")
    attachment_url = history.json()[0]["messages"][0]["attachment"]["url"]
    assert owner_client.get(attachment_url).status_code == 200

    intruder_client = _build_chat_test_client(session_factory, intruder)
    assert intruder_client.get(attachment_url).status_code == 404


def test_chat_stream_rejects_multiple_images(monkeypatch):
    session_factory = _build_session_factory()
    current_user = _create_student(session_factory)

    async def fake_stream_response(messages, fallback_text) -> AsyncIterator[str]:
        yield "先看题干。"

    monkeypatch.setattr(chat_router.llm_service, "stream_response", fake_stream_response)
    client = _build_chat_test_client(session_factory, current_user)
    response = client.post(
        "/api/chat/stream",
        data={"subject": "数学", "message": ""},
        files=[
            ("image", ("first.png", _make_test_image_bytes("red"), "image/png")),
            ("image", ("second.png", _make_test_image_bytes("blue"), "image/png")),
        ],
    )

    assert response.status_code == 400
    assert "Only one chat image is allowed" in response.text


def test_chat_attachment_file_is_removed_when_conversation_is_deleted(monkeypatch):
    session_factory = _build_session_factory()
    current_user = _create_student(session_factory)

    async def fake_stream_response(messages, fallback_text) -> AsyncIterator[str]:
        yield "先看图里的条件。"

    async def fake_understand(**kwargs):
        return ImageUnderstandingResult(
            filter_text="图示 条件",
            prompt_summary="图片里给出了题目的条件与图示。",
            ocr_raw_text="图示 条件",
            confidence_level="high",
            source="ocr",
            must_short_circuit=False,
        )

    monkeypatch.setattr(chat_router.llm_service, "stream_response", fake_stream_response)
    monkeypatch.setattr(chat_router.chat_image_understanding_service, "understand", fake_understand)
    monkeypatch.setattr(
        chat_router.rag_service,
        "retrieve",
        lambda db, subject, question, **kwargs: RetrievalResult(context="", chunks=[]),
    )
    monkeypatch.setattr(chat_router.question_cache_service, "is_cacheable", lambda **kwargs: False)

    client = _build_chat_test_client(session_factory, current_user)
    response = client.post(
        "/api/chat/stream",
        data={"subject": "数学", "message": ""},
        files={"image": ("cleanup.png", _make_test_image_bytes("purple"), "image/png")},
    )
    assert response.status_code == 200

    session = session_factory()
    try:
        attachment = session.scalar(select(ChatMessageAttachment))
        conversation = session.scalar(select(chat_router.Conversation))
        assert attachment is not None
        attachment_path = chat_router.chat_attachment_service.resolve_path(attachment.storage_key)
        assert attachment_path.exists()

        session.delete(conversation)
        session.commit()

        assert not attachment_path.exists()
    finally:
        session.close()


def test_chat_stream_cleans_up_saved_file_when_message_commit_fails(monkeypatch):
    session_factory = _build_session_factory()
    current_user = _create_student(session_factory)
    session = session_factory()
    saved: dict[str, str] = {}

    conversation_row = conversation.Conversation(student_id=current_user.id, subject="数学")
    session.add(conversation_row)
    session.commit()
    session.refresh(conversation_row)

    original_save_bytes = chat_router.chat_attachment_service.save_bytes

    def wrapped_save_bytes(**kwargs):
        stored = original_save_bytes(**kwargs)
        saved["storage_key"] = stored.storage_key
        return stored

    original_commit = session.commit
    failure_injected = {"done": False}

    def failing_commit():
        if not failure_injected["done"]:
            failure_injected["done"] = True
            raise RuntimeError("forced message commit failure")
        return original_commit()

    monkeypatch.setattr(chat_router.chat_attachment_service, "save_bytes", wrapped_save_bytes)
    monkeypatch.setattr(session, "commit", failing_commit)
    monkeypatch.setattr(chat_router.question_cache_service, "is_cacheable", lambda **kwargs: False)

    upload = UploadFile(
        file=BytesIO(_make_test_image_bytes("orange")),
        filename="rollback.png",
        headers=Headers({"content-type": "image/png"}),
    )

    try:
        try:
            asyncio.run(
                chat_router.stream_chat(
                    ChatRequest(subject="数学", message="", conversation_id=conversation_row.id),
                    session,
                    current_user,
                    FakeRequest(),
                    image_upload=upload,
                )
            )
        except RuntimeError as exc:
            assert "forced message commit failure" in str(exc)
        else:
            raise AssertionError("Expected forced message commit failure")

        assert session.scalar(select(Message)) is None
        assert session.scalar(select(ChatMessageAttachment)) is None
        attachment_path = chat_router.chat_attachment_service.resolve_path(saved["storage_key"])
        assert not attachment_path.exists()
    finally:
        session.close()


def test_chat_stream_keeps_saved_file_when_refresh_fails_after_commit(monkeypatch):
    session_factory = _build_session_factory()
    current_user = _create_student(session_factory)
    session = session_factory()
    saved: dict[str, str] = {}

    conversation_row = conversation.Conversation(student_id=current_user.id, subject="数学")
    session.add(conversation_row)
    session.commit()
    session.refresh(conversation_row)

    original_save_bytes = chat_router.chat_attachment_service.save_bytes

    def wrapped_save_bytes(**kwargs):
        stored = original_save_bytes(**kwargs)
        saved["storage_key"] = stored.storage_key
        return stored

    original_refresh = session.refresh
    failure_injected = {"done": False}

    def failing_refresh(instance, *args, **kwargs):
        if not failure_injected["done"]:
            failure_injected["done"] = True
            raise RuntimeError("forced refresh failure")
        return original_refresh(instance, *args, **kwargs)

    monkeypatch.setattr(chat_router.chat_attachment_service, "save_bytes", wrapped_save_bytes)
    monkeypatch.setattr(session, "refresh", failing_refresh)
    monkeypatch.setattr(chat_router.question_cache_service, "is_cacheable", lambda **kwargs: False)

    upload = UploadFile(
        file=BytesIO(_make_test_image_bytes("pink")),
        filename="refresh.png",
        headers=Headers({"content-type": "image/png"}),
    )

    try:
        try:
            asyncio.run(
                chat_router.stream_chat(
                    ChatRequest(subject="数学", message="", conversation_id=conversation_row.id),
                    session,
                    current_user,
                    FakeRequest(),
                    image_upload=upload,
                )
            )
        except RuntimeError as exc:
            assert "forced refresh failure" in str(exc)
        else:
            raise AssertionError("Expected forced refresh failure")

        inspection_session = session_factory()
        try:
            assert inspection_session.scalar(select(Message)) is not None
            assert inspection_session.scalar(select(ChatMessageAttachment)) is not None
        finally:
            inspection_session.close()

        attachment_path = chat_router.chat_attachment_service.resolve_path(saved["storage_key"])
        assert attachment_path.exists()
    finally:
        session.close()


def test_chat_stream_replay_request_id_distinguishes_different_images(monkeypatch):
    session_factory = _build_session_factory()
    current_user = _create_student(session_factory)

    async def fake_stream_response(messages, fallback_text) -> AsyncIterator[str]:
        yield "先看题干。"

    async def fake_understand(**kwargs):
        return ImageUnderstandingResult(
            filter_text="图像 已知条件",
            prompt_summary="图片提供了函数图像与已知条件。",
            ocr_raw_text="图像 已知条件",
            confidence_level="high",
            source="ocr",
            must_short_circuit=False,
        )

    monkeypatch.setattr(chat_router.llm_service, "stream_response", fake_stream_response)
    monkeypatch.setattr(chat_router.chat_image_understanding_service, "understand", fake_understand)
    monkeypatch.setattr(
        chat_router.rag_service,
        "retrieve",
        lambda db, subject, question, **kwargs: RetrievalResult(context="", chunks=[]),
    )
    monkeypatch.setattr(chat_router.question_cache_service, "is_cacheable", lambda **kwargs: False)
    monkeypatch.setattr(chat_router.request_replay_service, "store_backend", MemoryStore())

    client = _build_chat_test_client(session_factory, current_user)
    first = client.post(
        "/api/chat/stream",
        data={"subject": "数学", "message": "", "request_id": "same-image-request"},
        files={"image": ("first.png", _make_test_image_bytes("red"), "image/png")},
    )
    assert first.status_code == 200

    second = client.post(
        "/api/chat/stream",
        data={"subject": "数学", "message": "", "request_id": "same-image-request"},
        files={"image": ("second.png", _make_test_image_bytes("yellow"), "image/png")},
    )
    assert second.status_code == 409
    assert "different payload" in second.text


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
                "source_format": "docx",
                "source_locator": "question:1",
                "image_expectation": "required",
                "image_binding_status": "bound",
                "quality_flags": [],
                "question_uid": "7:question:1",
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
        assert result[0].assets[0].url == "/api/knowledge/documents/1/assets/image-001.png"
        assert result[0].assets[0].title == "受力图"
        assert result[0].answer_text is None
        assert result[0].explanation_text is None
        assert set(result[0].model_dump().keys()) == {
            "chunk_id",
            "document_id",
            "document_filename",
            "subject",
            "resource_type",
            "grade",
            "chapter",
            "section",
            "difficulty",
            "question_number",
            "question_text",
            "contains_images",
            "image_count",
            "assets",
            "answer_text",
            "explanation_text",
        }
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
        assert result[0].assets == []
    finally:
        session.close()


def test_recommend_questions_endpoint_excludes_disabled_rows(tmp_path, monkeypatch):
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

        disabled_row = KnowledgeChunk(
            document_id=document.id,
            subject="物理",
            chunk_index=0,
            content="第1题\n\n题目：斜面受力分析。",
            is_disabled=True,
            metadata_json={
                "document_id": document.id,
                "resource_type": document.resource_type,
                "grade": 2,
                "chunk_kind": "question_item",
                "question_number": "1",
                "question_text": "斜面受力分析。",
            },
        )
        enabled_row = KnowledgeChunk(
            document_id=document.id,
            subject="物理",
            chunk_index=1,
            content="第2题\n\n题目：匀速直线运动。",
            metadata_json={
                "document_id": document.id,
                "resource_type": document.resource_type,
                "grade": 2,
                "chunk_kind": "question_item",
                "question_number": "2",
                "question_text": "匀速直线运动。",
            },
        )
        session.add_all([disabled_row, enabled_row])
        session.commit()
        session.refresh(enabled_row)

        test_rag_service = _build_rag_service(tmp_path)
        monkeypatch.setattr(chat_router, "rag_service", test_rag_service)

        result = chat_router.recommend_questions(
            QuestionRecommendationRequest(
                subject="物理",
                question="再来一道运动题",
                limit=2,
            ),
            session,
            current_user,
        )

        assert [item.chunk_id for item in result] == [enabled_row.id]
        assert [item.question_number for item in result] == ["2"]
    finally:
        session.close()


def test_recommend_questions_passes_difficulty_preference_to_rag(monkeypatch):
    session_factory = _build_session_factory()
    current_user = _create_user(session_factory, role=UserRole.STUDENT, grade=2)
    session = session_factory()
    captured: dict[str, object] = {}
    try:
        monkeypatch.setattr(
            chat_router.rag_service,
            "recommend_questions",
            lambda db, subject, question, **kwargs: captured.update(
                {"subject": subject, "question": question, **kwargs}
            ) or [],
        )

        result = chat_router.recommend_questions(
            QuestionRecommendationRequest(
                subject="物理",
                question="牛顿第二定律练习题",
                difficulty_preference="advanced",
            ),
            session,
            current_user,
        )

        assert result == []
        assert captured["difficulty_preference"] == "advanced"
        assert captured["student_grade"] == 2
    finally:
        session.close()


def test_recommend_questions_can_use_conversation_context_seed(monkeypatch):
    session_factory = _build_session_factory()
    current_user = _create_user(session_factory, role=UserRole.STUDENT, grade=2)
    session = session_factory()
    captured: dict[str, object] = {}
    try:
        conversation = Conversation(student_id=current_user.id, subject="物理")
        session.add(conversation)
        session.commit()
        session.refresh(conversation)

        session.add_all(
            [
                Message(
                    conversation_id=conversation.id,
                    role=MessageRole.USER,
                    content="牛顿第二定律受力分析总是不会列方程",
                    turn_index=0,
                    guidance_stage=GuidanceStage.INITIAL,
                ),
                Message(
                    conversation_id=conversation.id,
                    role=MessageRole.ASSISTANT,
                    content="先说说你准备把哪些力画出来。",
                    turn_index=0,
                    guidance_stage=GuidanceStage.INITIAL,
                ),
                Message(
                    conversation_id=conversation.id,
                    role=MessageRole.USER,
                    content="请围绕下面这道题继续引导我，不要直接给答案：斜面上的木块受力分析和加速度怎么判断",
                    turn_index=1,
                    guidance_stage=GuidanceStage.HINT,
                ),
            ]
        )
        session.commit()

        monkeypatch.setattr(
            chat_router.rag_service,
            "recommend_questions",
            lambda db, subject, question, **kwargs: captured.update(
                {"subject": subject, "question": question, **kwargs}
            ) or [],
        )

        result = chat_router.recommend_questions(
            QuestionRecommendationRequest(
                subject="物理",
                recommendation_mode="context",
                conversation_id=conversation.id,
                difficulty_preference="standard",
            ),
            session,
            current_user,
        )

        assert result == []
        assert captured["subject"] == "物理"
        assert captured["student_grade"] == 2
        assert captured["difficulty_preference"] == "standard"
        assert captured["question"] == (
            "牛顿第二定律受力分析总是不会列方程；斜面上的木块受力分析和加速度怎么判断"
        )
    finally:
        session.close()


def test_recommendation_request_requires_conversation_id_for_context_mode():
    try:
        QuestionRecommendationRequest(
            subject="物理",
            recommendation_mode="context",
        )
    except ValidationError as exc:
        assert "Conversation id is required for context recommendations" in str(exc)
    else:
        raise AssertionError("Expected context recommendation payload to require conversation_id")
