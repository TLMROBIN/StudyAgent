from pathlib import Path


def test_model_manage_selects_new_account_after_account_creation():
    source = Path("frontend/src/views/ModelManage.vue").read_text()

    assert "const createdAccount = await createLLMProviderAccount" in source
    assert "modelForm.provider_account_id = createdAccount.id" in source


def test_student_chat_static_default_model_is_deepseek_flash():
    source = Path("frontend/src/views/StudentChat.vue").read_text()

    assert "{ key: 'deepseek-v4-flash', name: 'DeepSeek V4 Flash', description: '通用快捷' }" in source
    assert "llmModel: 'deepseek-v4-flash'" in source
    assert "chatModels.value[0]?.key || 'deepseek-v4-flash'" in source
