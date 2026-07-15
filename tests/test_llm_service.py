import asyncio

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.config import Settings
from backend.database import Base
from backend.models.llm_account import AccountBillingType, LLMProviderAccount
from backend.models.llm_model import LLMModelConfig
from backend.services.metrics_service import chat_image_vision_call_failures_total
from backend.services.llm_service import ThinkingContentFilter
from backend.services import llm_service as llm_service_module
from backend.services.llm_service import LLMService, LLMStreamEvent, LLMUsage, ProviderState


def _build_session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session_local = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    return testing_session_local


def _add_account(session, *, provider_name: str, enabled: bool = True) -> LLMProviderAccount:
    account = LLMProviderAccount(
        provider_name=provider_name,
        display_name=provider_name.title(),
        base_url=f"https://{provider_name}.example/v1",
        api_key=f"{provider_name}-secret",
        account_billing_type=AccountBillingType.PAY_AS_YOU_GO,
        is_enabled=enabled,
    )
    session.add(account)
    session.flush()
    return account


def test_thinking_filter_strips_complete_think_block():
    content_filter = ThinkingContentFilter()
    output = content_filter.feed("<think>先分析</think>最终回答")
    output += content_filter.flush()
    assert output == "最终回答"


def test_thinking_filter_handles_split_tags_across_chunks():
    content_filter = ThinkingContentFilter()
    output = content_filter.feed("<thi")
    output += content_filter.feed("nk>先分析")
    output += content_filter.feed("</thi")
    output += content_filter.feed("nk>最终")
    output += content_filter.feed("回答")
    output += content_filter.flush()
    assert output == "最终回答"


def test_leading_mode_tag_parser_strips_complete_tag():
    from backend.services.llm_service import LeadingModeTagParser

    parser = LeadingModeTagParser()
    mode, visible = parser.feed("<mode>fact</mode>直接结论。")

    assert mode == "fact"
    assert visible == "直接结论。"
    assert parser.flush() == ""


def test_leading_mode_tag_parser_handles_split_tag():
    from backend.services.llm_service import LeadingModeTagParser

    parser = LeadingModeTagParser()

    assert parser.feed("<mo") == (None, "")
    assert parser.feed("de>fa") == (None, "")
    assert parser.feed("ct</mode>结论") == ("fact", "结论")
    assert parser.feed("继续") == (None, "继续")


def test_leading_mode_tag_parser_defaults_to_guide_without_tag():
    from backend.services.llm_service import LeadingModeTagParser

    parser = LeadingModeTagParser()
    mode, visible = parser.feed("直接正文")

    assert mode == "guide"
    assert visible == "直接正文"


def test_leading_mode_tag_parser_flushes_partial_tag():
    from backend.services.llm_service import LeadingModeTagParser

    parser = LeadingModeTagParser()

    assert parser.feed("<mode>fa") == (None, "")
    assert parser.flush() == "<mode>fa"


def test_leading_mode_tag_parser_releases_overlong_prefix():
    from backend.services.llm_service import LeadingModeTagParser

    parser = LeadingModeTagParser()
    mode, visible = parser.feed(" " * 33)

    assert mode == "guide"
    assert visible == " " * 33


def test_stream_parser_reads_final_message_content_chunk(monkeypatch):
    service = LLMService()
    provider = ProviderState(
        name="minimax",
        base_url="https://api.example.test/v1",
        api_key="secret",
        model="MiniMax-M2.7-highspeed",
    )
    lines = [
        'data: {"choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}',
        'data: {"choices":[{"finish_reason":"stop","message":{"content":"先看图中的已知条件。"}}]}',
        "data: [DONE]",
    ]

    class FakeStreamResponse:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            for line in lines:
                yield line

    class FakeStreamContext:
        async def __aenter__(self):
            return FakeStreamResponse()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        def stream(self, *args, **kwargs):
            return FakeStreamContext()

    monkeypatch.setattr(llm_service_module.httpx, "AsyncClient", FakeAsyncClient)

    async def collect_chunks():
        return [chunk async for chunk in service._stream_openai_compatible(provider, [{"role": "user", "content": "看图"}])]

    import asyncio

    assert asyncio.run(collect_chunks()) == ["先看图中的已知条件。"]


def test_stream_parser_handles_cumulative_delta_content(monkeypatch):
    service = LLMService()
    provider = ProviderState(
        name="minimax",
        base_url="https://api.example.test/v1",
        api_key="secret",
        model="MiniMax-M2.7-highspeed",
    )
    lines = [
        'data: {"choices":[{"delta":{"content":"先看"}}]}',
        'data: {"choices":[{"delta":{"content":"先看图中"}}]}',
        'data: {"choices":[{"delta":{"content":"先看图中的已知条件。"}}]}',
        "data: [DONE]",
    ]

    class FakeStreamResponse:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            for line in lines:
                yield line

    class FakeStreamContext:
        async def __aenter__(self):
            return FakeStreamResponse()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        def stream(self, *args, **kwargs):
            return FakeStreamContext()

    monkeypatch.setattr(llm_service_module.httpx, "AsyncClient", FakeAsyncClient)

    async def collect_text():
        chunks = [chunk async for chunk in service._stream_openai_compatible(provider, [{"role": "user", "content": "看图"}])]
        return "".join(chunks)

    import asyncio

    assert asyncio.run(collect_text()) == "先看图中的已知条件。"


def test_stream_events_emit_chunks_and_final_usage(monkeypatch):
    service = LLMService()
    provider = ProviderState(
        name="deepseek",
        base_url="https://api.example.test/v1",
        api_key="secret",
        model="deepseek-chat",
    )
    lines = [
        'data: {"choices":[{"delta":{"content":"先看"}}]}',
        'data: {"choices":[{"delta":{"content":"条件。"}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":8,"total_tokens":20,"completion_tokens_details":{"reasoning_tokens":3},"prompt_cache_hit_tokens":2,"prompt_cache_miss_tokens":10}}',
        "data: [DONE]",
    ]
    captured_payloads: list[dict] = []

    class FakeStreamResponse:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            for line in lines:
                yield line

    class FakeStreamContext:
        async def __aenter__(self):
            return FakeStreamResponse()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        def stream(self, *args, **kwargs):
            captured_payloads.append(kwargs["json"])
            return FakeStreamContext()

    monkeypatch.setattr(llm_service_module.httpx, "AsyncClient", FakeAsyncClient)

    async def collect_events():
        return [
            event
            async for event in service._stream_openai_compatible_events(
                provider,
                [{"role": "user", "content": "看条件"}],
                max_completion_tokens=256,
            )
        ]

    import asyncio

    events = asyncio.run(collect_events())

    assert [(event.type, event.content) for event in events[:2]] == [("chunk", "先看"), ("chunk", "条件。")]
    assert events[-1].type == "usage"
    assert events[-1].usage.total_tokens == 20
    assert events[-1].usage.reasoning_tokens == 3
    assert captured_payloads[0]["stream_options"] == {"include_usage": True}
    assert captured_payloads[0]["max_completion_tokens"] == 256


def test_stream_response_falls_back_when_provider_only_emits_usage(monkeypatch):
    service = LLMService()
    provider = ProviderState(
        name="openrouter",
        base_url="https://api.example.test/v1",
        api_key="secret",
        model="deepseek/deepseek-v4-flash",
    )

    async def fake_stream_events(*args, **kwargs):
        yield LLMStreamEvent(type="usage", usage=LLMUsage(prompt_tokens=12, completion_tokens=0, total_tokens=12))

    monkeypatch.setattr(service, "_providers_for_chat_model", lambda model_key: [provider])
    monkeypatch.setattr(service, "_stream_openai_compatible_events", fake_stream_events)

    async def collect_chunks() -> list[str]:
        return [
            chunk
            async for chunk in service.stream_response(
                [{"role": "user", "content": "带电"}],
                "请继续说说你的想法。",
                model_key="minimax-m27",
            )
        ]

    assert asyncio.run(collect_chunks()) == ["请继续说说你的想法。"]


def test_builtin_chat_models_do_not_include_stopped_local_vl_model(monkeypatch):
    service = LLMService()
    monkeypatch.setattr(service, "_database_chat_model_options", lambda: [])

    options = service.chat_model_options()

    assert [item["key"] for item in options] == ["minimax-m27"]
    assert all("qwen2.5-vl" not in item["key"] for item in options)


def test_text_model_uses_enabled_vision_models_as_image_fallback_chain(monkeypatch):
    session_factory = _build_session_factory()
    session = session_factory()
    try:
        text_account = _add_account(session, provider_name="text")
        selected_vision_account = _add_account(session, provider_name="selected-vision")
        fallback_account = _add_account(session, provider_name="fallback-vision")
        disabled_account = _add_account(session, provider_name="disabled-vision", enabled=False)
        session.add_all(
            [
                LLMModelConfig(
                    model_key="minimax-m27",
                    display_name="Text Model",
                    provider_account_id=text_account.id,
                    provider_model="text-upstream",
                    capability_text=True,
                    capability_vision=False,
                    sort_order=1,
                ),
                LLMModelConfig(
                    model_key="vision-selected",
                    display_name="Selected Vision",
                    provider_account_id=selected_vision_account.id,
                    provider_model="vision-selected-upstream",
                    capability_text=True,
                    capability_vision=True,
                    sort_order=30,
                ),
                LLMModelConfig(
                    model_key="vision-fallback",
                    display_name="Fallback Vision",
                    provider_account_id=fallback_account.id,
                    provider_model="vision-fallback-upstream",
                    capability_text=True,
                    capability_vision=True,
                    sort_order=10,
                ),
                LLMModelConfig(
                    model_key="vision-disabled-account",
                    display_name="Disabled Account Vision",
                    provider_account_id=disabled_account.id,
                    provider_model="vision-disabled-upstream",
                    capability_text=True,
                    capability_vision=True,
                    sort_order=5,
                ),
            ]
        )
        session.commit()
    finally:
        session.close()

    service = LLMService()
    monkeypatch.setattr(service, "_session_factory", session_factory)

    text_model_providers = service._image_completion_providers("minimax-m27")
    selected_vision_providers = service._image_completion_providers("vision-selected")
    none_model_providers = service._image_completion_providers(None)

    assert [(provider.name, provider.model) for provider in text_model_providers] == [
        ("fallback-vision", "vision-fallback-upstream"),
        ("selected-vision", "vision-selected-upstream"),
    ]
    assert [(provider.name, provider.model) for provider in selected_vision_providers] == [
        ("selected-vision", "vision-selected-upstream"),
        ("fallback-vision", "vision-fallback-upstream"),
    ]
    assert [(provider.name, provider.model) for provider in none_model_providers] == [
        ("fallback-vision", "vision-fallback-upstream"),
        ("selected-vision", "vision-selected-upstream"),
    ]


def test_image_completion_provider_chain_is_empty_without_enabled_vision_models(monkeypatch):
    session_factory = _build_session_factory()
    session = session_factory()
    try:
        text_account = _add_account(session, provider_name="text")
        disabled_account = _add_account(session, provider_name="disabled-account", enabled=False)
        session.add_all(
            [
                LLMModelConfig(
                    model_key="minimax-m27",
                    display_name="Text Model",
                    provider_account_id=text_account.id,
                    provider_model="text-upstream",
                    capability_text=True,
                    capability_vision=False,
                    sort_order=1,
                ),
                LLMModelConfig(
                    model_key="disabled-vision",
                    display_name="Disabled Vision",
                    provider_account_id=disabled_account.id,
                    provider_model="disabled-upstream",
                    capability_text=True,
                    capability_vision=True,
                    sort_order=2,
                ),
            ]
        )
        session.commit()
    finally:
        session.close()

    service = LLMService()
    monkeypatch.setattr(service, "_session_factory", session_factory)

    assert service._image_completion_providers("minimax-m27") == []
    assert service._image_completion_providers(None) == []


def test_image_completion_logs_and_counts_http_failure(monkeypatch):
    service = LLMService()
    provider = ProviderState(
        name="vision",
        base_url="https://vision.example",
        api_key="vision-secret",
        model="vision-upstream",
    )
    warnings: list[str] = []
    monkeypatch.setattr(service, "_image_completion_providers", lambda model_key: [provider])

    async def fail_complete(provider, messages):
        request = httpx.Request("POST", "https://vision.example/v1/chat/completions")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

    monkeypatch.setattr(service, "_complete_openai_compatible", fail_complete)
    monkeypatch.setattr(
        llm_service_module.logger,
        "warning",
        lambda message, *args, **kwargs: warnings.append(message % args),
    )
    counter = chat_image_vision_call_failures_total.labels(reason="http_4xx")
    before = counter._value.get()

    result = asyncio.run(
        service._generate_image_completion(
            prompt="识别图片",
            image_bytes=b"fake-image",
            mime_type="image/png",
            model_key="minimax-m27",
        )
    )

    assert result == ""
    assert counter._value.get() == before + 1
    log_text = "\n".join(warnings)
    assert "chat_image_vision_call_failure" in log_text
    assert "provider=vision" in log_text
    assert "model=vision-upstream" in log_text
    assert "error_type=HTTPStatusError" in log_text
    assert "http_status=401" in log_text


def test_summarize_academic_image_requests_structured_json(monkeypatch):
    service = LLMService()
    captured: dict[str, object] = {}

    async def fake_generate_image_completion(**kwargs) -> str:
        captured.update(kwargs)
        return "{}"

    monkeypatch.setattr(service, "_generate_image_completion", fake_generate_image_completion)

    result = asyncio.run(
        service.summarize_academic_image(
            image_bytes=b"fake-image",
            mime_type="image/png",
            subject="物理",
            user_text="帮我看看",
            ocr_text="R=10Ω",
            model_key="vision-model",
        )
    )

    assert result == "{}"
    prompt = str(captured["prompt"])
    assert "输出 JSON" in prompt
    assert "无 markdown 围栏" in prompt
    for field in [
        "is_academic",
        "subject_guess",
        "question_text",
        "known_conditions",
        "options",
        "formulas_latex",
        "diagrams",
        "handwriting",
        "printed_answer",
        "uncertainties",
        "quality_issues",
    ]:
        assert field in prompt
    assert "不要直接给最终答案" in prompt


def test_image_completion_uses_vision_specific_timeout(monkeypatch):
    service = LLMService()
    provider = ProviderState(
        name="vision",
        base_url="https://vision.example",
        api_key="vision-secret",
        model="vision-upstream",
    )
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "题干：如图所示。"}}]}

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, *, headers, json, timeout):
            captured["url"] = url
            captured["payload"] = json
            captured["request_timeout"] = timeout
            return FakeResponse()

    monkeypatch.setattr(llm_service_module.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(service._complete_openai_compatible(provider, [{"role": "user", "content": "识别图片"}]))

    assert result == "题干：如图所示。"
    assert captured["url"] == "https://vision.example/v1/chat/completions"
    assert captured["timeout"].connect == 60
    assert captured["timeout"].read == 60
    assert captured["request_timeout"].connect == 60
    assert captured["request_timeout"].read == 60


def test_image_completion_uses_chat_image_vision_timeout_setting(monkeypatch):
    service = LLMService()
    service.settings = Settings(CHAT_IMAGE_VISION_TIMEOUT_SECONDS=77)
    provider = ProviderState(
        name="vision",
        base_url="https://vision.example",
        api_key="vision-secret",
        model="vision-upstream",
    )
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "题干：如图所示。"}}]}

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, *, headers, json, timeout):
            captured["request_timeout"] = timeout
            return FakeResponse()

    monkeypatch.setattr(llm_service_module.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(service._complete_openai_compatible(provider, [{"role": "user", "content": "识别图片"}]))

    assert result == "题干：如图所示。"
    assert captured["timeout"].connect == 77
    assert captured["timeout"].read == 77
    assert captured["request_timeout"].connect == 77
    assert captured["request_timeout"].read == 77


def test_llm_service_reuses_purpose_specific_http_clients_and_closes_them(monkeypatch):
    service = LLMService()
    clients: list[object] = []

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout
            self.is_closed = False
            clients.append(self)

        async def aclose(self):
            self.is_closed = True

    monkeypatch.setattr(llm_service_module.httpx, "AsyncClient", FakeAsyncClient)

    async def exercise_pool():
        chat_first = await service._get_chat_http_client()
        chat_second = await service._get_chat_http_client()
        vision_first = await service._get_vision_http_client()
        vision_second = await service._get_vision_http_client()
        assert chat_first is chat_second
        assert vision_first is vision_second
        assert chat_first is not vision_first
        await service.aclose()

    asyncio.run(exercise_pool())

    assert len(clients) == 2
    assert clients[0].timeout.connect == service.settings.llm_request_timeout_seconds
    assert clients[1].timeout.connect == service.settings.effective_chat_image_vision_timeout_seconds
    assert all(client.is_closed for client in clients)
    assert service._chat_http_client is None
    assert service._vision_http_client is None


def test_stream_events_pass_subject_llm_params_to_payload(monkeypatch):
    service = LLMService()
    provider = ProviderState(
        name="deepseek",
        base_url="https://api.example.test/v1",
        api_key="secret",
        model="deepseek-chat",
    )
    lines = ['data: {"choices":[{"delta":{"content":"好"}}]}', "data: [DONE]"]
    captured_payloads: list[dict] = []

    class FakeStreamResponse:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            for line in lines:
                yield line

    class FakeStreamContext:
        async def __aenter__(self):
            return FakeStreamResponse()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        def stream(self, *args, **kwargs):
            captured_payloads.append(kwargs["json"])
            return FakeStreamContext()

    monkeypatch.setattr(llm_service_module.httpx, "AsyncClient", FakeAsyncClient)

    import asyncio

    async def run_once(**kwargs):
        return [
            event
            async for event in service._stream_openai_compatible_events(
                provider,
                [{"role": "user", "content": "看条件"}],
                **kwargs,
            )
        ]

    # 分学科参数透传
    asyncio.run(run_once(temperature=0.15, top_p=0.95))
    assert captured_payloads[-1]["temperature"] == 0.15
    assert captured_payloads[-1]["top_p"] == 0.95

    # 不传 → 保持默认 0.3，且 payload 不含 top_p（现行为不变）
    asyncio.run(run_once())
    assert captured_payloads[-1]["temperature"] == 0.3
    assert "top_p" not in captured_payloads[-1]
