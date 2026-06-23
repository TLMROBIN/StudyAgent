from pathlib import Path


def test_post_deploy_check_verifies_frontend_bundle_assets() -> None:
    script = Path("scripts/post_deploy_check.sh").read_text(encoding="utf-8")

    assert "check_frontend_bundle_assets" in script
    assert "extract_frontend_assets" in script
    assert "Content-Type" in script
    assert "application/javascript" in script
    assert "text/css" in script


def test_frontend_publish_script_rebuilds_nginx_and_runs_post_deploy_check() -> None:
    script = Path("scripts/publish_frontend_bundle.sh").read_text(encoding="utf-8")

    assert "$COMPOSE build nginx" in script
    assert "$COMPOSE up -d --no-deps nginx" in script
    assert "scripts/post_deploy_check.sh" in script


def test_studyagent_deploy_verify_wraps_runtime_and_route_checks() -> None:
    script = Path("scripts/studyagent_deploy_verify.sh").read_text(encoding="utf-8")

    assert "scripts/post_deploy_check.sh" in script
    assert "/openapi.json" in script
    assert "EXPECTED_OPENAPI_PATHS" in script
    assert "REQUIRE_UPSTREAM_PARITY" in script
    assert "REQUIRE_CLEAN_WORKTREE" in script
    assert "VERIFY_TARGET" in script
    assert "REMOTE_HOST" in script
    assert "REQUIRE_REMOTE_HEAD_MATCH" in script
    assert "ssh -o BatchMode=yes" in script
