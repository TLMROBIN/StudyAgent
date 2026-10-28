from pathlib import Path


def test_nginx_serves_studyagent_prefixed_assets_without_spa_fallback() -> None:
    config = Path("nginx/nginx.conf").read_text(encoding="utf-8")

    assert "location /studyagent/assets/" in config
    prefixed_assets_block = config.split("location /studyagent/assets/", 1)[1].split("location", 1)[0]

    assert "alias /usr/share/nginx/html/assets/" in prefixed_assets_block
    assert "try_files $uri =404;" in prefixed_assets_block
    assert "/index.html" not in prefixed_assets_block


def test_nginx_limits_stream_connections_per_real_client_behind_school_gateway() -> None:
    config = Path("nginx/nginx.conf").read_text(encoding="utf-8")

    assert "set_real_ip_from 10.50.159.62;" in config
    assert "real_ip_header X-Forwarded-For;" in config
    assert "real_ip_recursive on;" in config
    assert config.index("real_ip_header X-Forwarded-For;") < config.index(
        "limit_conn_zone $binary_remote_addr zone=perip:10m;",
    )
    assert "limit_conn perip 2;" in config
