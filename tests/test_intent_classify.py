"""混合式意图分类服务（intent_classify_service）专项测试。

覆盖：LLM 正常返回候选代码 → override 生效；垃圾输出/超时/异常 → fail-open
返回 None；缓存命中不再调 LLM；候选白名单校验；chat.py 触发策略门控；
build_prompt 的 subject_mode_override 路由与等价性。
"""

from __future__ import annotations

import asyncio

import pytest

from backend.models.conversation import GuidanceStage
from backend.services.intent_classify_service import IntentClassifyService
from backend.services.socratic_service import socratic_service
from backend.services.store_service import MemoryStore
from backend.services.subject_guidance_service import SubjectTeachingMode, subject_guidance_service


class FakeLLM:
    def __init__(self, response: str | None = None, exc: Exception | None = None, delay: float = 0.0):
        self.response = response
        self.exc = exc
        self.delay = delay
        self.call_count = 0

    async def complete_response(self, messages, fallback_text="", **kwargs):
        self.call_count += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.response or ""


def _service(llm: FakeLLM) -> IntentClassifyService:
    return IntentClassifyService(store_backend=MemoryStore(), llm=llm)


def test_classify_returns_candidate_mode_on_valid_output():
    llm = FakeLLM(response="math_function")
    service = _service(llm)

    mode = asyncio.run(service.classify(question="这道题不会做", subject="数学"))

    assert mode == SubjectTeachingMode.MATH_FUNCTION
    assert llm.call_count == 1


def test_classify_tolerates_decorated_output():
    llm = FakeLLM(response="模式代码：`chinese_classical`。")
    service = _service(llm)

    mode = asyncio.run(service.classify(question="这段看不懂", subject="语文"))

    assert mode == SubjectTeachingMode.CHINESE_CLASSICAL


def test_classify_rejects_output_outside_subject_whitelist():
    # physics 模式代码不在数学候选集内 → 非法输出，fail-open
    llm = FakeLLM(response="force_diagram")
    service = _service(llm)

    assert asyncio.run(service.classify(question="这道题不会做", subject="数学")) is None


def test_classify_fails_open_on_exception_and_timeout():
    service_exc = _service(FakeLLM(exc=RuntimeError("provider down")))
    assert asyncio.run(service_exc.classify(question="这道题不会做", subject="数学")) is None

    slow = FakeLLM(response="math_problem", delay=0.2)
    service_slow = IntentClassifyService(store_backend=MemoryStore(), llm=slow)
    assert asyncio.run(service_slow.classify(question="这道题不会做", subject="数学", timeout_seconds=0.01)) is None


def test_classify_uses_cache_and_skips_llm_on_second_call():
    llm = FakeLLM(response="math_geometry")
    service = _service(llm)

    first = asyncio.run(service.classify(question="这道题怎么做", subject="数学"))
    second = asyncio.run(service.classify(question="这道题怎么做", subject="数学"))

    assert first == second == SubjectTeachingMode.MATH_GEOMETRY
    assert llm.call_count == 1


def test_classify_returns_none_for_unregistered_subject_or_empty_question():
    llm = FakeLLM(response="math_problem")
    service = _service(llm)

    assert asyncio.run(service.classify(question="随便", subject="体育")) is None
    assert asyncio.run(service.classify(question="   ", subject="数学")) is None
    assert llm.call_count == 0


# ---- analyze / build_prompt 的 override 路由 ----


def test_analyze_mode_override_routes_to_specified_rule():
    strategy = subject_guidance_service.analyze(
        "这道题不会做", "数学", GuidanceStage.HINT, mode_override=SubjectTeachingMode.MATH_FUNCTION
    )

    assert strategy is not None
    assert strategy.teaching_mode == SubjectTeachingMode.MATH_FUNCTION
    assert strategy.matched_by_trigger is True
    assert "数学函数专项模式" in strategy.prompt_section


def test_analyze_invalid_override_falls_back_to_rules():
    # 化学模式对数学无效 → 回落规则路由（兜底）
    strategy = subject_guidance_service.analyze(
        "这道题不会做", "数学", GuidanceStage.INITIAL, mode_override=SubjectTeachingMode.CHEM_EQUATION
    )

    assert strategy is not None
    assert strategy.teaching_mode == SubjectTeachingMode.MATH_PROBLEM
    assert strategy.matched_by_trigger is False


def test_build_prompt_with_mode_override_and_equivalence_without():
    with_override = socratic_service.build_prompt(
        "这道题不会做", "数学", [], "", "", subject_mode_override=SubjectTeachingMode.MATH_FUNCTION
    )
    without_a = socratic_service.build_prompt("这道题不会做", "数学", [], "", "")
    without_b = socratic_service.build_prompt("这道题不会做", "数学", [], "", "", subject_mode_override=None)

    assert "数学函数专项模式" in with_override.messages[0]["content"]
    # 不传 override 时与现行为逐字一致
    assert without_a.messages == without_b.messages
    assert without_a.fallback_text == without_b.fallback_text


# ---- chat.py 触发策略门控 ----


def _run_gate(monkeypatch, *, guidance_params, question="这道题不会做", subject="数学", history=None,
              has_image_turn=False, practice_review_turn=False):
    from backend.routers import chat as chat_router

    calls = []

    async def fake_classify(**kwargs):
        calls.append(kwargs)
        return SubjectTeachingMode.MATH_FUNCTION

    monkeypatch.setattr(chat_router.intent_classify_service, "classify", fake_classify)
    result = asyncio.run(
        chat_router._classify_subject_mode(
            guidance_params=guidance_params,
            question=question,
            subject=subject,
            history_pairs=history or [],
            has_image_turn=has_image_turn,
            practice_review_turn=practice_review_turn,
            model_key=None,
        )
    )
    return result, calls


def test_gate_disabled_by_default(monkeypatch):
    result, calls = _run_gate(monkeypatch, guidance_params=None)
    assert result is None and calls == []

    result, calls = _run_gate(monkeypatch, guidance_params={"intent_classifier": {"enabled": False}})
    assert result is None and calls == []


def test_gate_triggers_only_for_low_confidence_rule_match(monkeypatch):
    enabled = {"intent_classifier": {"enabled": True}}
    # 兜底规则命中（低置信）→ 触发分类
    result, calls = _run_gate(monkeypatch, guidance_params=enabled, question="这道题不会做")
    assert result == SubjectTeachingMode.MATH_FUNCTION and len(calls) == 1

    # 触发词命中（高置信）→ 不触发
    result, calls = _run_gate(monkeypatch, guidance_params=enabled, question="帮我配平这个化学方程式", subject="化学")
    assert result is None and calls == []


def test_gate_skips_physics_image_practice_and_subject_whitelist(monkeypatch):
    enabled = {"intent_classifier": {"enabled": True}}

    result, calls = _run_gate(monkeypatch, guidance_params=enabled, subject="物理")
    assert result is None and calls == []

    result, calls = _run_gate(monkeypatch, guidance_params=enabled, has_image_turn=True)
    assert result is None and calls == []

    result, calls = _run_gate(monkeypatch, guidance_params=enabled, practice_review_turn=True)
    assert result is None and calls == []

    whitelist = {"intent_classifier": {"enabled": True, "subjects": ["语文"]}}
    result, calls = _run_gate(monkeypatch, guidance_params=whitelist, subject="数学")
    assert result is None and calls == []


def test_question_cache_key_includes_teaching_mode_only_when_set():
    from backend.services.question_cache_service import QuestionCacheService

    service = QuestionCacheService(store_backend=MemoryStore())
    common = dict(
        subject="数学",
        question="这道函数题怎么做",
        guidance_stage=GuidanceStage.INITIAL,
        agent_version=1,
        chunks=[],
        llm_model="default",
    )
    base_key = service._build_key(**common)
    override_key = service._build_key(**common, teaching_mode="math_function")
    none_key = service._build_key(**common, teaching_mode=None)

    assert base_key == none_key  # 不生效时 key 不变（存量缓存零失效）
    assert override_key != base_key
