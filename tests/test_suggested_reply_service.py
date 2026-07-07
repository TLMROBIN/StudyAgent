import asyncio

from backend.models.conversation import GuidanceStage
from backend.services.suggested_reply_service import SuggestedReplyService


class FakeLLM:
    async def complete_response(self, *args, **kwargs):
        return """
        <think>内部推理不应泄露</think>
        {"replies": [
          "我觉得先看定义域，但还没想清理由",
          "最终答案是 A",
          "是不是要比较两个区间的变化",
          "继续",
          "能不能提示我先验证哪一步"
        ]}
        """


class PartialPhysicsLLM:
    async def complete_response(self, *args, **kwargs):
        return '{"replies": ["两个带电物体之间吗？"]}'


class EmptyLLM:
    async def complete_response(self, *args, **kwargs):
        return '{"replies": []}'


def test_suggested_reply_service_filters_direct_answers_and_generic_replies():
    service = SuggestedReplyService(llm=FakeLLM())

    replies = asyncio.run(
        service.generate(
            subject="数学",
            guidance_stage=GuidanceStage.INITIAL,
            current_question="函数单调性第一步怎么想",
            assistant_response="先看定义域，再想函数在哪些区间上讨论。你觉得第一步应该圈出什么？",
            history=[],
            model_key="minimax-m27",
        )
    )

    assert replies == [
        "我觉得先看定义域，但还没想清理由",
        "是不是要比较两个区间的变化",
        "能不能提示我先验证哪一步",
    ]


def test_suggested_reply_service_supplements_partial_concept_replies():
    service = SuggestedReplyService(llm=PartialPhysicsLLM())

    replies = asyncio.run(
        service.generate(
            subject="物理",
            guidance_stage=GuidanceStage.INITIAL,
            current_question="讲解下库仑定律",
            assistant_response="库仑定律描述的是哪两个物体之间的相互作用？这两个物体需要满足什么条件？",
            history=[],
            model_key="deepseek-v4-flash",
        )
    )

    assert len(replies) >= 3
    assert "两个带电物体之间吗？" in replies
    assert "是不是还要看能不能近似成点电荷？" in replies
    assert all("最终答案" not in reply and "答案选" not in reply for reply in replies)


def test_suggested_reply_service_generates_safe_fallback_when_model_returns_empty():
    service = SuggestedReplyService(llm=EmptyLLM())

    replies = asyncio.run(
        service.generate(
            subject="数学",
            guidance_stage=GuidanceStage.HINT,
            current_question="函数单调性第一步怎么想",
            assistant_response="你觉得第一步应该先圈出什么条件？",
            history=[],
            model_key="minimax-m27",
        )
    )

    assert replies == [
        "我先说一个关键词，你帮我判断方向对不对。",
        "是不是还需要补充一个适用条件？",
        "我不确定下一步该看哪个条件。",
    ]
