from pathlib import Path


def test_student_chat_refreshes_model_quota_after_stream_finishes():
    source = Path("frontend/src/views/StudentChat.vue").read_text()
    start = source.index("await streamChat(")
    end = source.index("} catch (error)", start)
    success_block = source[start:end]

    assert "await loadChatModels()" in success_block


def test_student_chat_renders_and_sends_suggested_replies():
    source = Path("frontend/src/views/StudentChat.vue").read_text()

    assert "if (event === 'suggested_replies')" in source
    assert "last.suggested_replies = normalizeSuggestedReplies(data.suggested_replies)" in source
    assert "canShowSuggestedReplies(index, item)" in source
    assert "@click=\"sendSuggestedReply(reply)\"" in source


def test_student_chat_uses_selected_image_without_opening_crop_dialog_by_default():
    source = Path("frontend/src/views/StudentChat.vue").read_text()
    start = source.index("function handleImageSelection")
    end = source.index("function removePendingImage", start)
    selection_handler = source[start:end]

    assert "updatePendingImage(prepared.file)" in selection_handler
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


def test_student_chat_prepares_selected_images_before_upload():
    source = Path("frontend/src/views/StudentChat.vue").read_text()
    start = source.index("async function handleImageSelection")
    end = source.index("function removePendingImage", start)
    selection_handler = source[start:end]

    assert "prepareChatImageUpload(file)" in selection_handler
    assert "updatePendingImage(prepared.file)" in selection_handler
    assert "qualityWarnings" in selection_handler
    assert "ElMessage.error" in selection_handler


def test_chat_image_upload_utility_compresses_to_jpeg_and_checks_quality():
    source = Path("frontend/src/utils/chatImageUpload.ts").read_text()

    assert "MAX_CHAT_IMAGE_LONG_EDGE = 2000" in source
    assert "CHAT_IMAGE_JPEG_QUALITY = 0.85" in source
    assert "MAX_CHAT_IMAGE_BYTES = 8 * 1024 * 1024" in source
    assert "image/jpeg" in source
    assert "canvas.toBlob" in source
    assert "laplacianVariance" in source
    assert "averageBrightness" in source
    assert "照片可能模糊/过暗，建议重拍" in source
    assert "请在相册中将照片导出为 JPG 后上传" in source
