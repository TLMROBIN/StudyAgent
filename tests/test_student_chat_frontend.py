from pathlib import Path


def test_student_chat_refreshes_model_quota_after_stream_finishes():
    source = Path("frontend/src/views/StudentChat.vue").read_text()
    start = source.index("await streamChat(")
    end = source.index("} catch (error)", start)
    success_block = source[start:end]

    assert "await loadChatModels()" in success_block
