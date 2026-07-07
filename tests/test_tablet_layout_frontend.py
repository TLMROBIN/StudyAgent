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
