from pathlib import Path


def test_student_chat_refreshes_model_quota_after_stream_finishes():
    source = Path("frontend/src/views/StudentChat.vue").read_text()
    start = source.index("await streamChat(")
    end = source.index("} catch (error)", start)
    success_block = source[start:end]

    assert "await loadChatModels()" in success_block


def test_student_chat_uses_selected_image_without_opening_crop_dialog_by_default():
    source = Path("frontend/src/views/StudentChat.vue").read_text()
    start = source.index("function handleImageSelection")
    end = source.index("function removePendingImage", start)
    selection_handler = source[start:end]

    assert "updatePendingImage(file)" in selection_handler
    assert "openCropDialog(file)" not in selection_handler


def test_student_chat_initial_crop_selection_covers_full_image():
    source = Path("frontend/src/views/StudentChat.vue").read_text()
    start = source.index("function initializeCropSelection")
    end = source.index("function cropSelectionStyle", start)
    crop_initializer = source[start:end]

    assert "* 0.82" not in crop_initializer
    assert "cropSelection.x = 0" in crop_initializer
    assert "cropSelection.y = 0" in crop_initializer
    assert "cropSelection.width = Math.max(1, Math.round(width))" in crop_initializer
    assert "cropSelection.height = Math.max(1, Math.round(height))" in crop_initializer
