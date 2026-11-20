"""本地话题决策模型（topic_model_service）与 classify_topic 接入测试。

覆盖：HTTP 结果分档；超时/异常/越界输出返回 None；off 模式不发请求；
enforce 高/低置信直接定案、中间档与故障回退 LLM；输出侧默认不 enforce；
shadow 模式返回值与 LLM 一致且只记录对比。
"""

from __future__ import annotations

import asyncio
import json
import httpx

from backend.services import intent_classify_service as intent_module
from backend.services.first_refusal_audit import trace_first_question
from backend.services.intent_classify_service import IntentClassifyService
from backend.services.store_service import MemoryStore
from backend.services.topic_model_service import TopicModelService


class FakeLLM:
    def __init__(self, response: str = "academic"):
        self.response = response
        self.call_count = 0

    async def complete_response(self, messages, fallback_text="", **kwargs):
        self.call_count += 1
        return self.response


def _transport(p_academic=None, *, exc: Exception | None = None, status: int = 200, seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        if exc is not None:
            raise exc
        return httpx.Response(status, json={"p_academic": p_academic, "model_version": "laya-ft-test"})

    return httpx.MockTransport(handler)


def _model(mode: str, transport: httpx.MockTransport, **kwargs) -> TopicModelService:
    return TopicModelService(
        mode=mode,
        url="http://topic-model.test/classify",
        timeout_seconds=0.3,
        accept_threshold=0.9,
        reject_threshold=0.05,
        enforce_output=kwargs.pop("enforce_output", False),
        transport=transport,
    )


def _service(model: TopicModelService, llm: FakeLLM) -> IntentClassifyService:
    return IntentClassifyService(store_backend=MemoryStore(), llm=llm, topic_model=model)


def test_predict_bands_by_threshold_and_sends_context():
    seen: list = []
    model = _model("enforce", _transport(0.95, seen=seen))
    result = asyncio.run(model.predict(text="那第二问呢", subject="数学", context="求函数单调区间"))

    assert result.band == "academic"
    assert result.model_version == "laya-ft-test"
    assert seen == [{"subject": "数学", "text": "那第二问呢", "context": "求函数单调区间", "kind": "input"}]
    assert model.band_for(0.5) == "uncertain"
    assert model.band_for(0.05) == "off_topic"


def test_predict_returns_none_on_failure_or_invalid_output():
    for transport in (
        _transport(exc=httpx.ReadTimeout("slow")),
        _transport(exc=httpx.ConnectError("down")),
        _transport(0.9, status=500),
        _transport(1.7),
        _transport("abc"),
    ):
        assert asyncio.run(_model("shadow", transport).predict(text="x", subject="数学")) is None


def test_off_mode_or_missing_url_disables_model():
    seen: list = []
    assert not _model("off", _transport(0.99, seen=seen)).enabled
    assert not TopicModelService(mode="enforce", url="", transport=_transport(0.99, seen=seen)).enabled
    assert not TopicModelService(mode="bogus", url="http://x/classify", transport=_transport(0.99, seen=seen)).enabled

    llm = FakeLLM("academic")
    service = _service(_model("off", _transport(0.0, seen=seen)), llm)
    assert asyncio.run(service.classify_topic(question="函数单调性怎么判断", subject="数学")) == "academic"
    assert seen == []
    assert llm.call_count == 1


def test_enforce_confident_bands_skip_llm():
    llm = FakeLLM("academic")
    service = _service(_model("enforce", _transport(0.01)), llm)
    assert asyncio.run(service.classify_topic(question="帮我写一篇同人小说", subject="语文")) == "off_topic"

    service = _service(_model("enforce", _transport(0.97)), llm)
    assert asyncio.run(service.classify_topic(question="函数单调性怎么判断", subject="数学")) == "academic"
    assert llm.call_count == 0


def test_enforce_uncertain_or_failed_model_falls_back_to_llm():
    llm = FakeLLM("off_topic")
    service = _service(_model("enforce", _transport(0.5)), llm)
    assert asyncio.run(service.classify_topic(question="这个怎么弄", subject="数学")) == "off_topic"

    service = _service(_model("enforce", _transport(exc=httpx.ConnectError("down"))), llm)
    assert asyncio.run(service.classify_topic(question="那个怎么弄", subject="数学")) == "off_topic"
    assert llm.call_count == 2


def test_enforce_output_kind_only_when_enabled():
    llm = FakeLLM("academic")
    service = _service(_model("enforce", _transport(0.01)), llm)
    assert asyncio.run(service.classify_topic(question="学生请求：x\n待输出回复：y", subject="数学", kind="output")) == "academic"
    assert llm.call_count == 1

    service = _service(_model("enforce", _transport(0.01), enforce_output=True), llm)
    assert asyncio.run(service.classify_topic(question="学生请求：x\n待输出回复：z", subject="数学", kind="output")) == "off_topic"
    assert llm.call_count == 1


def test_shadow_returns_llm_verdict_and_logs_hash_only(monkeypatch):
    llm = FakeLLM("academic")
    service = _service(_model("shadow", _transport(0.01)), llm)
    logged: list[str] = []
    monkeypatch.setattr(intent_module.logger, "info", lambda msg, *args: logged.append(msg % args))

    async def run():
        verdict = await service.classify_topic(question="帮我写一篇同人小说", subject="语文")
        await asyncio.sleep(0.05)
        return verdict

    assert asyncio.run(run()) == "academic"

    shadow_logs = [line for line in logged if "topic_model_shadow" in line]
    assert len(shadow_logs) == 1
    assert "band=off_topic" in shadow_logs[0] and "llm_verdict=academic" in shadow_logs[0]
    assert "同人小说" not in shadow_logs[0]
    assert not service._shadow_tasks


def test_accept_only_defers_low_scores_and_output_to_existing_classifier():
    for score, kind, allow_fast_accept, expected, calls in (
        (0.97, "input", True, "academic", 0),
        (0.01, "input", True, "academic", 1),
        (0.5, "input", True, "off_topic", 1),
        (0.97, "output", True, "off_topic", 1),
        (0.97, "input", False, "off_topic", 1),
    ):
        llm = FakeLLM(expected if calls else "off_topic")
        service = _service(_model("accept_only", _transport(score), enforce_output=True), llm)
        verdict = asyncio.run(service.classify_topic_with_source(
            question="学科候选", subject="数学", kind=kind, allow_fast_accept=allow_fast_accept,
        ))
        assert verdict.label == expected
        assert verdict.verified_by_llm == bool(calls)
        assert llm.call_count == calls


def test_contextual_review_bypasses_laya_and_caches_each_context_independently():
    seen = []
    llm = FakeLLM("academic")
    service = _service(_model("accept_only", _transport(0.99, seen=seen)), llm)

    async def classify(context, prompt):
        return await service.classify_topic_with_source(
            question="继续", subject="语文", context=context, followup_prompt=prompt,
        )

    first = asyncio.run(classify("分析议论文结构", "你能分析下一段论证吗？"))
    llm.response = "off_topic"
    switched = asyncio.run(classify("请聊聊明星绯闻", "接下来还想聊谁？"))
    repeated = asyncio.run(classify("分析议论文结构", "你能分析下一段论证吗？"))
    assert (first.label, switched.label, repeated.label) == ("academic", "off_topic", "academic")
    assert first.verified_by_llm
    assert llm.call_count == 2
    assert not seen



def test_contextual_specialist_accepts_only_eligible_high_scores():
    for score, eligible, expected, calls in (
        (0.5848, True, "academic", 0),
        (0.5847, True, "off_topic", 1),
        (0.99, False, "off_topic", 1),
    ):
        seen = []
        llm = FakeLLM("off_topic")
        specialist = TopicModelService(
            mode="accept_only", url="http://short-topic-model.test/classify",
            accept_threshold=0.5848, transport=_transport(score, seen=seen),
        )
        service = IntentClassifyService(
            store_backend=MemoryStore(), llm=llm,
            topic_model=_model("accept_only", _transport(0.99)),
            contextual_model=specialist,
        )

        async def classify():
            try:
                return await service.classify_topic_with_source(
                    question="向右", subject="物理", context="分析物块受力",
                    followup_prompt="加速度指向哪边？", contextual_model_eligible=eligible,
                )
            finally:
                await service.aclose()

        verdict = asyncio.run(classify())
        assert verdict.label == expected
        assert verdict.verified_by_llm == bool(calls)
        assert llm.call_count == calls
        assert bool(seen) == eligible
        if seen:
            assert seen[0]["context"] == "学生上一问：分析物块受力\n导师问句：加速度指向哪边？"
            assert seen[0]["kind"] == "input"


def test_contextual_review_keeps_tutor_question_before_long_explanation():
    class CapturingLLM(FakeLLM):
        async def complete_response(self, messages, fallback_text="", **kwargs):
            self.payload = json.loads(messages[1]["content"])
            return await super().complete_response(messages, fallback_text, **kwargs)

    llm = CapturingLLM()
    service = _service(_model("off", _transport()), llm)
    tutor_question = "你通常先看代词前面的内容还是后面的内容？"
    explanation = "先读完整段落，核对句子之间的逻辑。" * 30
    result = asyncio.run(service.classify_topic_with_source(
        question="第二个名词", subject="英语", context="如何判断代词所指的对象？",
        followup_prompt=tutor_question + "\n" + explanation,
    ))

    assert result.label == "academic"
    excerpt = llm.payload["导师上一轮回复"]
    assert tutor_question in excerpt
    assert excerpt.endswith(explanation[-30:])
    assert len(excerpt) <= 360



def test_contextual_shadow_uses_tutor_question_and_cannot_override_existing_verdict(monkeypatch):
    seen = []
    service = _service(_model("shadow", _transport(0.99, seen=seen)), FakeLLM("off_topic"))
    comparisons = []

    def record(task, **kwargs):
        comparisons.append((task.result().band, kwargs["kind"], kwargs["llm_verdict"]))
        service._shadow_tasks.discard(task)

    monkeypatch.setattr(service, "_record_shadow", record)

    async def run():
        args = dict(
            question="第二个名词", subject="英语", context="怎样判断代词指向谁？",
            followup_prompt="哪个名词与代词一致？" + "核对前后句。" * 80,
        )
        for eligible in (False, True):
            verdict = await service.classify_topic_with_source(**args, contextual_model_eligible=eligible)
            assert verdict.label == "off_topic"
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(run())
    assert len(seen) == 1
    assert "学生上一问：怎样判断代词指向谁？" in seen[0]["context"]
    assert "导师问句：哪个名词与代词一致？" in seen[0]["context"]
    assert "核对前后句" not in seen[0]["context"]
    assert seen[0]["kind"] == "input"
    assert comparisons == [("academic", "contextual_input", "off_topic")]



def test_accept_only_uses_existing_remote_verdict_before_local_inference():
    requests = []
    llm = FakeLLM("off_topic")
    model = _model("off", _transport(0.99, seen=requests))
    service = _service(model, llm)

    async def run():
        question = "推荐一个游戏"
        original = await service.classify_topic(question=question, subject="数学")
        assert original == "off_topic"
        model.mode = "accept_only"
        with trace_first_question(True) as trace:
            cached = await service.classify_topic_with_source(question=question, subject="数学")
        assert trace["source"] == "cache"
        assert trace["classifier_model_key"]
        assert cached.label == "off_topic"
        assert cached.verified_by_llm
        fresh = await service.classify_topic_with_source(question="怎么求概率？", subject="数学")
        assert fresh.label == "academic"
        assert not fresh.verified_by_llm
        await service.aclose()

    asyncio.run(run())
    assert llm.call_count == 1
    assert len(requests) == 1
