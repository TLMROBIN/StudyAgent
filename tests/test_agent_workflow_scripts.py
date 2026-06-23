from pathlib import Path


def test_agents_contract_requires_remote_verification_and_recovery_probe() -> None:
    text = Path("AGENTS.md").read_text(encoding="utf-8")

    assert "scripts/studyagent_deploy_verify.sh" in text
    assert "REQUIRE_REMOTE_HEAD_MATCH=1" in text
    assert "scripts/studyagent_recover_context.sh" in text
    assert "只读恢复现场" in text


def test_recover_context_script_checks_local_and_remote_state() -> None:
    script = Path("scripts/studyagent_recover_context.sh").read_text(encoding="utf-8")

    assert "git status --short --branch" in script
    assert "git worktree list" in script
    assert "ssh -o BatchMode=yes" in script
    assert "/home/binyu/文档/trae_projects/StudyAgent" in script
    assert "docker compose ps" in script
    assert "/openapi.json" in script
