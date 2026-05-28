from collections.abc import AsyncIterator
import asyncio
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base, get_db
from backend.dependencies import get_current_user
from backend.models import agent_config, audit_log, conversation, knowledge, user  # noqa: F401
from backend.models.llm_account import AccountBillingType, LLMProviderAccount
from backend.models.llm_model import LLMModelConfig, LLMQuotaPolicy, QuotaBillingMode
from backend.models.llm_provider import LLMProviderConfig
from backend.models.llm_usage import LLMUsageEvent
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


def test_llm_quota_models_create_relationships_in_sqlite():
    session_factory = _build_session_factory()
    session = session_factory()
    try:
        admin = _create_admin(session_factory)
        account = LLMProviderAccount(
            provider_name="minimax",
            display_name="MiniMax Token Plan",
            base_url="https://api.minimax.chat/v1",
            api_key="secret",
            account_billing_type=AccountBillingType.TOKEN_PLAN,
            created_by=admin.id,
        )
        session.add(account)
        session.flush()
        model = LLMModelConfig(
            model_key="minimax-m27",
            display_name="MiniMax M2.7",
            description="高速答疑模型",
            provider_account_id=account.id,
            provider_model="MiniMax-M2.7-highspeed",
            is_primary=True,
            created_by=admin.id,
        )
        session.add(model)
        session.flush()
        policy = LLMQuotaPolicy(
            model_config_id=model.id,
            billing_mode=QuotaBillingMode.REQUEST_COUNT,
            user_daily_request_limit=20,
            provider_rolling_5h_request_limit=500,
        )
        session.add(policy)
        session.flush()
        usage = LLMUsageEvent(
            user_id=admin.id,
            request_id="request-1",
            model_config_id=model.id,
            provider_account_id=account.id,
            model_key=model.model_key,
            provider_name=account.provider_name,
            provider_model=model.provider_model,
            billing_mode=policy.billing_mode.value,
            request_count=1,
            source="request_count",
            reservation_key="quota:reservation:request-1",
        )
        session.add(usage)
        session.commit()

        stored_model = session.get(LLMModelConfig, model.id)
        assert stored_model is not None
        assert stored_model.provider_account.display_name == "MiniMax Token Plan"
        assert stored_model.quota_policy.billing_mode == QuotaBillingMode.REQUEST_COUNT
        assert stored_model.usage_events[0].request_id == "request-1"
    finally:
        session.close()


def test_admin_can_create_provider_account_and_request_count_model():
    session_factory = _build_session_factory()
    client = _build_provider_test_client(session_factory, _create_admin(session_factory))

    account_response = client.post(
        "/api/llm-providers/accounts",
        json={
            "provider_name": "minimax",
            "display_name": "MiniMax Token Plan",
            "base_url": "https://api.minimax.chat/v1",
            "api_key": "secret",
            "account_billing_type": "token_plan",
            "is_enabled": True,
        },
    )

    assert account_response.status_code == 201
    assert account_response.json()["has_api_key"] is True
    assert "api_key" not in account_response.json()

    model_response = client.post(
        "/api/llm-providers/models",
        json={
            "model_key": "minimax-m27",
            "display_name": "MiniMax M2.7",
            "description": "高速答疑模型",
            "provider_account_id": account_response.json()["id"],
            "provider_model": "MiniMax-M2.7-highspeed",
            "capability_text": True,
            "capability_vision": True,
            "vision_understanding_priority": True,
            "is_enabled": True,
            "is_primary": True,
            "is_fallback": False,
            "sort_order": 10,
            "quota_policy": {
                "billing_mode": "request_count",
                "user_daily_request_limit": 20,
                "provider_rolling_5h_request_limit": 500,
                "count_cache_hit": False,
            },
        },
    )

    assert model_response.status_code == 201
    body = model_response.json()
    assert body["model_key"] == "minimax-m27"
    assert body["capability_vision"] is True
    assert body["vision_understanding_priority"] is True
    assert body["quota_policy"]["billing_mode"] == "request_count"
    assert body["quota_policy"]["user_daily_request_limit"] == 20


def test_admin_can_delete_model_config_with_usage_events():
    session_factory = _build_session_factory()
    admin = _create_admin(session_factory)
    client = _build_provider_test_client(session_factory, admin)

    account_response = client.post(
        "/api/llm-providers/accounts",
        json={
            "provider_name": "minimax",
            "display_name": "MiniMax Token Plan",
            "base_url": "https://api.minimax.chat/v1",
            "api_key": "secret",
            "account_billing_type": "token_plan",
            "is_enabled": True,
        },
    )
    model_response = client.post(
        "/api/llm-providers/models",
        json={
            "model_key": "minimax-m27",
            "display_name": "MiniMax M2.7",
            "description": "高速答疑模型",
            "provider_account_id": account_response.json()["id"],
            "provider_model": "MiniMax-M2.7-highspeed",
            "is_enabled": True,
            "is_primary": True,
            "is_fallback": False,
            "sort_order": 10,
            "quota_policy": {
                "billing_mode": "request_count",
                "user_daily_request_limit": 20,
                "count_cache_hit": False,
            },
        },
    )
    model_id = model_response.json()["id"]
    account_id = account_response.json()["id"]
    session = session_factory()
    try:
        usage = LLMUsageEvent(
            user_id=admin.id,
            request_id="request-before-delete",
            model_config_id=model_id,
            provider_account_id=account_id,
            model_key="minimax-m27",
            provider_name="minimax",
            provider_model="MiniMax-M2.7-highspeed",
            billing_mode="request_count",
            request_count=1,
            source="request_count",
        )
        session.add(usage)
        session.commit()
    finally:
        session.close()

    delete_response = client.delete(f"/api/llm-providers/models/{model_id}")

    assert delete_response.status_code == 204
    assert client.get("/api/llm-providers/models").json() == []


def test_empty_provider_account_api_key_is_rejected_on_create_and_ignored_on_update():
    session_factory = _build_session_factory()
    client = _build_provider_test_client(session_factory, _create_admin(session_factory))

    rejected = client.post(
        "/api/llm-providers/accounts",
        json={
            "provider_name": "minimax",
            "display_name": "MiniMax",
            "base_url": "https://api.minimax.chat/v1",
            "api_key": " ",
            "account_billing_type": "token_plan",
        },
    )
    assert rejected.status_code == 422

    created = client.post(
        "/api/llm-providers/accounts",
        json={
            "provider_name": "minimax",
            "display_name": "MiniMax",
            "base_url": "https://api.minimax.chat/v1",
            "api_key": "secret",
            "account_billing_type": "token_plan",
        },
    )
    account_id = created.json()["id"]

    updated = client.put(
        f"/api/llm-providers/accounts/{account_id}",
        json={
            "provider_name": "minimax",
            "display_name": "MiniMax Updated",
            "base_url": "https://api.minimax.chat/v1",
            "api_key": "",
            "account_billing_type": "token_plan",
            "is_enabled": True,
        },
    )

    assert updated.status_code == 200
    assert updated.json()["display_name"] == "MiniMax Updated"


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


def test_llm_service_tries_database_fallback_model_when_selected_model_fails(monkeypatch):
    session_factory = _build_session_factory()
    session = session_factory()
    try:
        primary_account = LLMProviderAccount(
            provider_name="minimax",
            display_name="MiniMax",
            base_url="https://primary.example/v1",
            api_key="primary-secret",
            account_billing_type=AccountBillingType.TOKEN_PLAN,
        )
        fallback_account = LLMProviderAccount(
            provider_name="qwen",
            display_name="Qwen",
            base_url="https://fallback.example/v1",
            api_key="fallback-secret",
            account_billing_type=AccountBillingType.PAY_AS_YOU_GO,
        )
        session.add_all([primary_account, fallback_account])
        session.flush()
        primary_model = LLMModelConfig(
            model_key="minimax-m27",
            display_name="MiniMax M2.7",
            provider_account_id=primary_account.id,
            provider_model="MiniMax-M2.7-highspeed",
            is_primary=True,
            sort_order=10,
        )
        fallback_model = LLMModelConfig(
            model_key="qwen-plus",
            display_name="Qwen Plus",
            provider_account_id=fallback_account.id,
            provider_model="qwen-plus",
            is_fallback=True,
            sort_order=20,
        )
        session.add_all([primary_model, fallback_model])
        session.commit()
    finally:
        session.close()

    service = LLMService()
    monkeypatch.setattr(service, "_session_factory", session_factory)
    seen: list[tuple[str, str]] = []

    async def fake_stream(provider, messages) -> AsyncIterator[str]:
        seen.append((provider.name, provider.model))
        if provider.name == "minimax":
            raise RuntimeError("primary failed")
        yield "备用模型响应"

    monkeypatch.setattr(service, "_stream_openai_compatible", fake_stream)

    async def collect_chunks() -> list[str]:
        return [
            chunk
            async for chunk in service.stream_response(
                [{"role": "user", "content": "函数题怎么想"}],
                "兜底",
                model_key="minimax-m27",
            )
        ]

    chunks = asyncio.run(collect_chunks())

    assert chunks == ["备用模型响应"]
    assert seen == [("minimax", "MiniMax-M2.7-highspeed"), ("qwen", "qwen-plus")]


def test_llm_service_uses_selected_vision_model_for_image_understanding(monkeypatch):
    session_factory = _build_session_factory()
    session = session_factory()
    try:
        account = LLMProviderAccount(
            provider_name="vision",
            display_name="Vision Provider",
            base_url="https://vision.example/v1",
            api_key="vision-secret",
            account_billing_type=AccountBillingType.PAY_AS_YOU_GO,
        )
        session.add(account)
        session.flush()
        model = LLMModelConfig(
            model_key="vision-model",
            display_name="Vision Model",
            provider_account_id=account.id,
            provider_model="vision-upstream",
            capability_vision=True,
            vision_understanding_priority=True,
            sort_order=10,
        )
        session.add(model)
        session.commit()
    finally:
        session.close()

    service = LLMService()
    monkeypatch.setattr(service, "_session_factory", session_factory)

    providers = service._image_completion_providers("vision-model")

    assert service.prefers_vision_understanding("vision-model") is True
    assert [(provider.name, provider.base_url, provider.model) for provider in providers] == [
        ("vision", "https://vision.example/v1", "vision-upstream")
    ]


def test_llm_service_exposes_builtin_student_chat_models():
    service = LLMService()

    options = service.chat_model_options()

    assert [
        (item["key"], item["name"], item["description"]) for item in options
    ] == [
        ("minimax-m27", "MiniMax-M2.7", "highspeed"),
    ]


def test_llm_service_rejects_stopped_builtin_local_vl_model():
    service = LLMService()

    with pytest.raises(ValueError, match="Unsupported chat model"):
        service.normalize_chat_model_key("qwen2.5-vl")


def test_llm_service_logs_empty_stream_fallback(monkeypatch, caplog):
    service = LLMService()
    service.providers[0].api_key = "test-key"

    async def fake_stream(provider, messages) -> AsyncIterator[str]:
        if False:
            yield ""

    monkeypatch.setattr(service, "_stream_openai_compatible", fake_stream)

    async def collect_chunks() -> list[str]:
        return [chunk async for chunk in service.stream_response([{"role": "user", "content": "看图"}], "兜底")]

    with caplog.at_level(logging.WARNING, logger="backend.services.llm_service"):
        chunks = asyncio.run(collect_chunks())

    assert chunks == ["兜底"]
    assert "empty_stream" in caplog.text
    assert "MiniMax-M2.7-highspeed" in caplog.text


def test_llm_service_requires_configured_vision_model_for_image_completion(monkeypatch):
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

    assert asyncio.run(extract_text()) == ""
    assert seen == []


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

    assert asyncio.run(extract_text()) == ""
    assert seen == []


def test_llm_service_reports_chat_model_statuses(monkeypatch):
    service = LLMService()

    async def fake_probe(provider):
        return True, ""

    monkeypatch.setattr(service, "_probe_openai_compatible", fake_probe)

    async def collect_statuses():
        return await service.chat_model_statuses(force_refresh=True)

    statuses = asyncio.run(collect_statuses())

    assert [(item["key"], item["status"], item["message"]) for item in statuses] == [
        ("minimax-m27", "available", ""),
    ]


def test_llm_service_model_statuses_do_not_probe_by_default(monkeypatch):
    service = LLMService()

    async def fail_probe(provider):
        raise AssertionError("default model status checks must not call external providers")

    monkeypatch.setattr(service, "_probe_openai_compatible", fail_probe)

    async def collect_statuses():
        return await service.chat_model_statuses()

    statuses = asyncio.run(collect_statuses())

    assert [item["key"] for item in statuses] == ["minimax-m27"]
    assert {item["status"] for item in statuses} <= {"available", "unavailable"}


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


def test_llm_probe_reports_upstream_quota_separately_from_user_quota(monkeypatch):
    service = LLMService()
    provider = service.providers[0]
    provider.api_key = "primary-secret"

    class FakeResponse:
        status_code = 402

        def raise_for_status(self):
            request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("payment required", request=request, response=response)

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, *, headers, json):
            return FakeResponse()

    monkeypatch.setattr("backend.services.llm_service.httpx.AsyncClient", FakeAsyncClient)

    async def check():
        return await service._probe_openai_compatible(provider)

    ok, message = asyncio.run(check())

    assert ok is False
    assert "上游模型额度不足或请求被上游限流" in message
    assert "你今天" not in message
