from backend.services.llm_service import ThinkingContentFilter
from backend.services import llm_service as llm_service_module
from backend.services.llm_service import LLMService, ProviderState


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
