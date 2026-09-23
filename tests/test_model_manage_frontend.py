from pathlib import Path


def test_model_manage_selects_new_account_after_account_creation():
    source = Path("frontend/src/views/ModelManage.vue").read_text()

    assert "const createdAccount = await createLLMProviderAccount" in source
    assert "modelForm.provider_account_id = createdAccount.id" in source


def test_student_chat_static_default_model_is_glm_flash():
    source = Path("frontend/src/views/StudentChat.vue").read_text()

    assert "{ key: 'glm-5.3-flash-chat', name: 'GLM-5.3-Flash', description: '高中答疑' }" in source
    assert "llmModel: 'glm-5.3-flash-chat'" in source
    assert "chatModels.value[0]?.key || 'glm-5.3-flash-chat'" in source
