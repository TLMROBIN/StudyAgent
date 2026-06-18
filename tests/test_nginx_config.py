from pathlib import Path


def test_nginx_serves_studyagent_prefixed_assets_without_spa_fallback() -> None:
    config = Path("nginx/nginx.conf").read_text(encoding="utf-8")

    assert "location /studyagent/assets/" in config
    prefixed_assets_block = config.split("location /studyagent/assets/", 1)[1].split("location", 1)[0]

    assert "alias /usr/share/nginx/html/assets/" in prefixed_assets_block
    assert "try_files $uri =404;" in prefixed_assets_block
    assert "/index.html" not in prefixed_assets_block
