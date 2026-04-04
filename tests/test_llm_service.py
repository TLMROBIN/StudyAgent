from backend.services.llm_service import ThinkingContentFilter


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
