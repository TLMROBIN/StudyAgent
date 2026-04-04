from backend.models.conversation import summarize_conversation_topic


def test_summarize_conversation_topic_trims_prefill_prefix():
    topic = summarize_conversation_topic(
        "数学",
        "请围绕下面这道题继续引导我，不要直接给答案：已知函数 y=x^2+2x-3，求最小值",
    )

    assert topic == "已知函数 y=x^2+2x-3，求最小值"


def test_summarize_conversation_topic_falls_back_to_subject():
    assert summarize_conversation_topic("物理", "   ") == "物理答疑"
