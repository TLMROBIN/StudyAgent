from collections.abc import AsyncIterator
import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base, get_db
from backend.dependencies import get_current_user
from backend.models import agent_config, audit_log, conversation, knowledge, user  # noqa: F401
from backend.models.llm_provider import LLMProviderConfig
from backend.models.user import User, UserRole
from backend.routers import llm_provider
from backend.services.llm_service import LLMService


def _build_session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    return TestingSessionLocal


def _create_admin(session_factory) -> User:
    session = session_factory()
    try:
        admin = User(
            username="admin",
            full_name="管理员",
            role=UserRole.ADMIN,
            password_hash="fake-hash",
            must_change_password=False,
            is_active=True,
        )
        session.add(admin)
        session.commit()
        session.refresh(admin)
        session.expunge(admin)
        return admin
    finally:
        session.close()


def _build_provider_test_client(session_factory, current_user: User) -> TestClient:
    app = FastAPI()
    app.include_router(llm_provider.router)

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    def override_current_user():
        return current_user

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_current_user
    return TestClient(app)


def test_admin_can_create_and_select_llm_providers_without_revealing_api_keys():
    session_factory = _build_session_factory()
    client = _build_provider_test_client(session_factory, _create_admin(session_factory))

    primary_response = client.post(
        "/api/llm-providers/",
        json={
            "name": "MiniMax",
            "base_url": "https://api.minimax.chat/v1",
            "api_key": "primary-secret",
            "model": "MiniMax-M2.7-highspeed",
        },
    )
    fallback_response = client.post(
        "/api/llm-providers/",
        json={
            "name": "Qwen",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key": "fallback-secret",
            "model": "qwen-plus",
        },
    )

    assert primary_response.status_code == 201
    assert fallback_response.status_code == 201

    primary_id = primary_response.json()["id"]
    fallback_id = fallback_response.json()["id"]
    select_response = client.post(
        "/api/llm-providers/selection",
        json={"active_provider_id": primary_id, "fallback_provider_id": fallback_id},
    )

    assert select_response.status_code == 200
    providers = client.get("/api/llm-providers/").json()
    assert [(item["name"], item["is_active"], item["is_fallback"]) for item in providers] == [
        ("MiniMax", True, False),
        ("Qwen", False, True),
    ]
    assert all("api_key" not in item for item in providers)
    assert all(item["has_api_key"] for item in providers)


def test_llm_service_uses_selected_database_providers_before_environment(monkeypatch):
    session_factory = _build_session_factory()
    session = session_factory()
    try:
        primary = LLMProviderConfig(
            name="Selected",
            base_url="https://selected.example/v1",
            api_key="selected-secret",
            model="selected-model",
            is_active=True,
        )
        fallback = LLMProviderConfig(
            name="Fallback",
            base_url="https://fallback.example/v1",
            api_key="fallback-secret",
            model="fallback-model",
            is_fallback=True,
        )
        session.add_all([primary, fallback])
        session.commit()
    finally:
        session.close()

    service = LLMService()
    monkeypatch.setattr(service, "_session_factory", session_factory)
    seen: list[tuple[str, str, str]] = []

    async def fake_stream(provider, messages) -> AsyncIterator[str]:
        seen.append((provider.name, provider.base_url or "", provider.model))
        yield "数据库模型响应"

    monkeypatch.setattr(service, "_stream_openai_compatible", fake_stream)

    async def collect_chunks() -> list[str]:
        return [chunk async for chunk in service.stream_response([{"role": "user", "content": "你好"}], "兜底")]

    chunks = asyncio.run(collect_chunks())

    assert chunks == ["数据库模型响应"]
    assert seen == [("Selected", "https://selected.example/v1", "selected-model")]


def test_llm_service_exposes_builtin_student_chat_models():
    service = LLMService()

    options = service.chat_model_options()

    assert [
        (item["key"], item["name"], item["description"]) for item in options
    ] == [
        ("minimax-m27", "MiniMax-M2.7", "highspeed"),
        ("qwen2.5-vl", "qwen2.5-vl", "图片理解推荐使用，但响应速度可能较慢。"),
    ]


def test_llm_service_can_stream_with_builtin_local_vl_model(monkeypatch):
    service = LLMService()
    seen: list[tuple[str, str, str, str]] = []

    async def fake_stream(provider, messages) -> AsyncIterator[str]:
        seen.append((provider.name, provider.base_url or "", provider.api_key or "", provider.model))
        yield "本地模型响应"

    monkeypatch.setattr(service, "_stream_openai_compatible", fake_stream)

    async def collect_chunks() -> list[str]:
        return [
            chunk
            async for chunk in service.stream_response(
                [{"role": "user", "content": "看图题怎么入手"}],
                "兜底",
                model_key="qwen2.5-vl",
            )
        ]

    chunks = asyncio.run(collect_chunks())

    assert chunks == ["本地模型响应"]
    assert seen == [
        ("qwen2.5-vl", "http://10.50.159.63:8001/v1", "EMPTY", "qwen2.5-vl-72b-instruct")
    ]


def test_llm_service_uses_vision_provider_for_image_completion(monkeypatch):
    service = LLMService()
    service.providers[0].api_key = "primary-secret"
    seen: list[str] = []

    monkeypatch.setattr(
        service,
        "_runtime_providers",
        lambda: [
            service.providers[0],
        ],
    )

    async def fake_complete(provider, messages):
        seen.append(provider.name)
        return "图片题干"

    monkeypatch.setattr(service, "_complete_openai_compatible", fake_complete)

    async def extract_text():
        return await service.extract_image_text(
            image_bytes=b"fake-image",
            mime_type="image/png",
            subject="数学",
        )

    assert asyncio.run(extract_text()) == "图片题干"
    assert seen == ["qwen2.5-vl"]


def test_llm_service_does_not_send_images_to_minimax_m2(monkeypatch):
    service = LLMService()
    service.providers[0].api_key = "primary-secret"
    seen: list[str] = []

    async def fake_complete(provider, messages):
        seen.append(provider.name)
        return "题干：已知函数图像经过点 A"

    monkeypatch.setattr(service, "_complete_openai_compatible", fake_complete)

    async def extract_text():
        return await service.extract_image_text(
            image_bytes=b"fake-image",
            mime_type="image/png",
            subject="数学",
        )

    assert asyncio.run(extract_text()) == "题干：已知函数图像经过点 A"
    assert seen == ["qwen2.5-vl"]


def test_llm_service_reports_chat_model_statuses(monkeypatch):
    service = LLMService()

    async def fake_probe(provider):
        if provider.name == "qwen2.5-vl":
            return False, "连接失败"
        return True, ""

    monkeypatch.setattr(service, "_probe_openai_compatible", fake_probe)

    async def collect_statuses():
        return await service.chat_model_statuses(force_refresh=True)

    statuses = asyncio.run(collect_statuses())

    assert [(item["key"], item["status"], item["message"]) for item in statuses] == [
        ("minimax-m27", "available", ""),
        ("qwen2.5-vl", "unavailable", "连接失败"),
    ]


def test_llm_probe_uses_m2_completion_tokens_and_full_timeout(monkeypatch):
    service = LLMService()
    provider = service.providers[0]
    provider.api_key = "primary-secret"
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, *, headers, json):
            captured["url"] = url
            captured["payload"] = json
            return FakeResponse()

    monkeypatch.setattr("backend.services.llm_service.httpx.AsyncClient", FakeAsyncClient)

    async def check():
        return await service._probe_openai_compatible(provider)

    ok, message = asyncio.run(check())

    assert ok is True
    assert message == ""
    payload = captured["payload"]
    assert payload["max_completion_tokens"] == 8
    assert "max_tokens" not in payload
    assert captured["timeout"].connect == service.settings.llm_request_timeout_seconds
