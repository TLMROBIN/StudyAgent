from pathlib import Path


def test_admin_role_route_and_navigation_are_admin_only():
    router = Path("frontend/src/router/index.ts").read_text(encoding="utf-8")
    app = Path("frontend/src/App.vue").read_text(encoding="utf-8")

    assert "'/admin/roles'" in router
    assert "roles: ['admin']" in router
    assert "教学角色" in app


def test_admin_role_editor_uses_structured_fields_without_freeform_prompt():
    source = Path("frontend/src/views/AgentRoleManage.vue").read_text(encoding="utf-8")

    assert "form.style_config.tone" in source
    assert "form.style_config.explanation_pace" in source
    assert "form.style_config.analogy_style" in source
    assert "multiple-limit=\"3\"" in source
    assert "system_prompt" not in source
    assert "temperature" not in source
    assert "top_p" not in source


def test_student_chat_loads_subject_roles_and_sends_selected_role_id():
    chat = Path("frontend/src/views/StudentChat.vue").read_text(encoding="utf-8")
    api = Path("frontend/src/utils/api.ts").read_text(encoding="utf-8")

    assert "'/agent-roles/enabled'" in chat
    assert "v-model=\"form.roleId\"" in chat
    assert "role_id: form.roleId" in chat
    assert "formData.append('role_id'" in api
    assert "subject_mismatch" in chat
