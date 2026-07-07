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
