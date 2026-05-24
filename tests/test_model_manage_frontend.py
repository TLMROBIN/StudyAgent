from pathlib import Path


def test_model_manage_selects_new_account_after_account_creation():
    source = Path("frontend/src/views/ModelManage.vue").read_text()

    assert "const createdAccount = await createLLMProviderAccount" in source
    assert "modelForm.provider_account_id = createdAccount.id" in source
