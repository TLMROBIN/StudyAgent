from pathlib import Path


def test_message_table_cells_wrap_instead_of_nowrap():
    css = Path("frontend/src/styles.css").read_text()
    start = css.index(".message-body th,")
    end = css.index("}", start)
    cell_rule = css[start:end]

    assert "white-space: normal" in cell_rule
    assert "nowrap" not in cell_rule


def test_message_body_children_can_shrink_inside_grid():
    css = Path("frontend/src/styles.css").read_text()

    assert ".message-body > *" in css
    assert "min-width: 0" in css[css.index(".message-body > *") : css.index("}", css.index(".message-body > *"))]


def test_inline_katex_has_overflow_handling():
    css = Path("frontend/src/styles.css").read_text()

    assert ":not(.katex-display) > .katex" in css


def test_viewport_meta_declares_interactive_widget():
    html = Path("frontend/index.html").read_text()

    assert "interactive-widget=resizes-content" in html


def test_chat_input_scrolls_into_view_on_focus():
    source = Path("frontend/src/views/StudentChat.vue").read_text()

    assert "ref=\"messageInputRef\"" in source
    assert "@focus=\"handleMessageInputFocus\"" in source
    assert "handleMessageInputFocus" in source
    assert "scrollIntoView" in source


def test_student_chat_main_uses_resized_viewport_height():
    app = Path("frontend/src/App.vue").read_text()
    css = Path("frontend/src/styles.css").read_text()

    assert "app-main--student-chat" in app
    assert "route.path === '/student'" in app

    start = css.index(".app-main--student-chat")
    end = css.index("}", start)
    rule = css[start:end]
    assert "height: var(--app-viewport-height" in rule
    assert "max-height: var(--app-viewport-height" in rule
    assert "overflow: hidden" in rule


def test_rich_text_strips_leading_blank_lines():
    source = Path("frontend/src/utils/richText.ts").read_text()
    start = source.index("function collapseSoftLineBreaks")
    end = source.index("function renderMarkdownBlocks", start)
    block = source[start:end]

    assert "normalizedLines.shift()" in block
    assert "normalizedLines.pop()" in block


def test_student_chat_trims_first_streaming_chunk():
    source = Path("frontend/src/views/StudentChat.vue").read_text()
    start = source.index("if (event === 'chunk')")
    end = source.index("if (event === 'done')", start)
    chunk_block = source[start:end]

    assert "data.content.replace(/^\\s+/, '')" in chunk_block


def test_user_bubbles_tone_down_role_label_with_subdued_color():
    css = Path("frontend/src/styles.css").read_text()

    assert ".bubble.user .bubble-role" in css
    start = css.index(".bubble.user .bubble-role")
    end = css.index("}", start)
    rule = css[start:end]
    assert "color: rgba(255, 255, 255, 0.78)" in rule
    assert "display: none" not in rule
