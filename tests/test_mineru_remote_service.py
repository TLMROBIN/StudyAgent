import io
import json
from pathlib import Path
import zipfile

import pytest

from backend.config import Settings
from backend.services.mineru_remote_service import MineruRemoteService
from backend.services.mineru_service import (
    MineruStartupError,
    MineruTransientIOError,
)


class FakeResponse:
    def __init__(self, status_code: int = 200, json_body: dict | None = None, content: bytes = b""):
        self.status_code = status_code
        self._json_body = json_body
        self.content = content

    def json(self):
        if self._json_body is None:
            raise ValueError("no json body")
        return self._json_body


def build_service(tmp_path: Path, **overrides) -> MineruRemoteService:
    kwargs = dict(
        PDF_PARSER_BACKEND="mineru_remote",
        TASK_ARTIFACT_PATH=str(tmp_path / "tasks"),
        MINERU_LANG="ch",
        MINERU_REMOTE_API_KEY="official-token",
        MINERU_REMOTE_302_API_KEY="302-token",
        MINERU_REMOTE_POLL_INTERVAL_SECONDS=0,
        MINERU_REMOTE_TIMEOUT_SECONDS=30,
    )
    kwargs.update(overrides)
    return MineruRemoteService(settings=Settings(**kwargs))


def build_result_zip() -> bytes:
    content_list = [
        [
            {"type": "paragraph", "content": {"paragraph_content": [{"type": "text", "content": "第一题 已知受力图。"}]}},
            {
                "type": "image",
                "content": {
                    "image_source": {"path": "images/pic.png"},
                    "image_caption": ["示意图"],
                },
            },
        ]
    ]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("demo/demo_content_list.json", json.dumps(content_list, ensure_ascii=False))
        archive.writestr("demo/images/pic.png", b"\x89PNG-fake-bytes")
    return buffer.getvalue()


def write_source_pdf(tmp_path: Path) -> Path:
    source_file = tmp_path / "demo.pdf"
    source_file.write_bytes(b"%PDF-1.4")
    return source_file


def install_fake_send(service: MineruRemoteService, handler, calls: list):
    def fake_send(method, url, *, headers=None, json_body=None, params=None, content=None):
        calls.append({"method": method, "url": url, "headers": headers, "json_body": json_body, "params": params, "has_content": content is not None})
        # Route through the same status classification as the real _send.
        return MineruRemoteService._classify_response(handler(method, url), url)

    return fake_send


def test_official_provider_success_path_unpacks_zip_and_assembles_assets(tmp_path, monkeypatch):
    service = build_service(tmp_path)
    source_file = write_source_pdf(tmp_path)
    zip_bytes = build_result_zip()
    calls: list = []

    def handler(method, url):
        if url.endswith("/api/v4/file-urls/batch"):
            return FakeResponse(json_body={"code": 0, "data": {"batch_id": "batch-1", "file_urls": ["https://oss.example/upload-1"]}})
        if url == "https://oss.example/upload-1":
            return FakeResponse()
        if "/api/v4/extract-results/batch/batch-1" in url:
            return FakeResponse(json_body={"code": 0, "data": {"extract_result": [{"state": "done", "full_zip_url": "https://cdn.example/result.zip"}]}})
        if url == "https://cdn.example/result.zip":
            return FakeResponse(content=zip_bytes)
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(service, "_send", install_fake_send(service, handler, calls))

    result = service.parse_pdf(str(source_file), task_id=9, document_id=7)

    assert result.parser_backend == "mineru-remote-official"
    assert "第一题 已知受力图。" in result.text
    assert "[[asset:image-001]]" in result.text
    assert result.assets[0].asset_id == "image-001"
    stored_asset = Path(result.assets[0].storage_path)
    assert stored_asset.is_file()
    assert stored_asset.parent == tmp_path / "tasks" / "knowledge" / "7"
    assert stored_asset.read_bytes() == b"\x89PNG-fake-bytes"
    assert result.parser_provenance["provider"] == "official"
    assert result.parser_provenance["provider_attempts"] == [{"provider": "official", "status": "ok"}]

    methods_urls = [(call["method"], call["url"]) for call in calls]
    assert ("POST", "https://mineru.net/api/v4/file-urls/batch") in methods_urls
    assert ("PUT", "https://oss.example/upload-1") in methods_urls
    # OSS upload must be a raw binary PUT (no Content-Type header semantics).
    put_call = next(call for call in calls if call["method"] == "PUT")
    assert put_call["has_content"] is True


def test_network_timeout_degrades_to_next_provider(tmp_path, monkeypatch):
    service = build_service(
        tmp_path,
        MINERU_REMOTE_PROVIDERS="official,302_paid",
        MINERU_REMOTE_PUBLIC_BASE_URL="http://files.lan/static",
    )
    source_file = write_source_pdf(tmp_path)
    zip_bytes = build_result_zip()
    calls: list = []

    def handler(method, url):
        if "mineru.net" in url:
            return FakeResponse(status_code=504)
        if url.endswith("/302/v2/mineru/task") and method == "POST":
            return FakeResponse(json_body={"code": 0, "data": {"task_id": "t-302"}})
        if url.endswith("/302/v2/mineru/task") and method == "GET":
            return FakeResponse(json_body={"code": 0, "data": {"state": "done", "full_zip_url": "https://cdn.example/result.zip"}})
        if url == "https://cdn.example/result.zip":
            return FakeResponse(content=zip_bytes)
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(service, "_send", install_fake_send(service, handler, calls))

    result = service.parse_pdf(str(source_file), task_id=9, document_id=7)

    assert result.parser_backend == "mineru-remote-302_paid"
    attempts = result.parser_provenance["provider_attempts"]
    assert attempts[0]["provider"] == "official"
    assert attempts[0]["status"] == "failed"
    assert "504" in attempts[0]["reason"]
    assert attempts[1] == {"provider": "302_paid", "status": "ok"}

    # Official provider was retried once before degrading (2 POST attempts).
    official_posts = [c for c in calls if c["url"].endswith("/api/v4/file-urls/batch")]
    assert len(official_posts) == 2
    # 302 paid provider received a file URL built from MINERU_REMOTE_PUBLIC_BASE_URL.
    paid_post = next(c for c in calls if c["url"].endswith("/302/v2/mineru/task") and c["method"] == "POST")
    assert paid_post["json_body"]["pdf_url"].startswith("http://files.lan/static/")


def test_auth_failure_does_not_degrade(tmp_path, monkeypatch):
    service = build_service(
        tmp_path,
        MINERU_REMOTE_PROVIDERS="official,302_paid",
        MINERU_REMOTE_PUBLIC_BASE_URL="http://files.lan/static",
    )
    source_file = write_source_pdf(tmp_path)
    calls: list = []

    def handler(method, url):
        if "mineru.net" in url:
            return FakeResponse(status_code=401)
        raise AssertionError("302 provider must not be attempted after auth failure")

    monkeypatch.setattr(service, "_send", install_fake_send(service, handler, calls))

    with pytest.raises(MineruStartupError, match="401"):
        service.parse_pdf(str(source_file), task_id=9, document_id=7)

    assert all("mineru.net" in call["url"] for call in calls)


def test_remote_parse_failure_state_does_not_degrade(tmp_path, monkeypatch):
    service = build_service(tmp_path, MINERU_REMOTE_PROVIDERS="official,302_free")
    source_file = write_source_pdf(tmp_path)
    calls: list = []

    def handler(method, url):
        if url.endswith("/api/v4/file-urls/batch"):
            return FakeResponse(json_body={"code": 0, "data": {"batch_id": "batch-1", "file_urls": ["https://oss.example/upload-1"]}})
        if url == "https://oss.example/upload-1":
            return FakeResponse()
        if "/api/v4/extract-results/batch/batch-1" in url:
            return FakeResponse(json_body={"code": 0, "data": {"extract_result": [{"state": "failed", "err_msg": "corrupt pdf"}]}})
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(service, "_send", install_fake_send(service, handler, calls))

    with pytest.raises(MineruStartupError, match="corrupt pdf"):
        service.parse_pdf(str(source_file), task_id=9, document_id=7)


def test_all_providers_failed_raises_transient_io_error(tmp_path, monkeypatch):
    service = build_service(tmp_path, MINERU_REMOTE_PROVIDERS="official")
    source_file = write_source_pdf(tmp_path)
    calls: list = []

    def handler(method, url):
        return FakeResponse(status_code=429)

    monkeypatch.setattr(service, "_send", install_fake_send(service, handler, calls))

    with pytest.raises(MineruTransientIOError, match="All MinerU remote providers failed"):
        service.parse_pdf(str(source_file), task_id=9, document_id=7)

    # retried once → 2 calls total
    assert len(calls) == 2


def test_302_providers_skipped_without_public_base_url(tmp_path, monkeypatch):
    service = build_service(tmp_path, MINERU_REMOTE_PROVIDERS="302_free,302_paid")
    source_file = write_source_pdf(tmp_path)

    monkeypatch.setattr(
        service,
        "_send",
        lambda *args, **kwargs: pytest.fail("no HTTP request should be made when providers are skipped"),
    )

    with pytest.raises(MineruTransientIOError, match="PUBLIC_BASE_URL"):
        service.parse_pdf(str(source_file), task_id=9, document_id=7)

    snapshot = service.health_snapshot()
    assert snapshot["parser_backend"] == "mineru_remote"
    assert snapshot["provider_status"]["302_free"]["configured"] is False
    assert "PUBLIC_BASE_URL" in snapshot["provider_status"]["302_free"]["skipped_reason"]


def test_transient_quota_error_code_degrades(tmp_path, monkeypatch):
    service = build_service(tmp_path, MINERU_REMOTE_PROVIDERS="official")
    source_file = write_source_pdf(tmp_path)

    def handler(method, url):
        return FakeResponse(json_body={"code": -60009, "msg": "queue full"})

    monkeypatch.setattr(service, "_send", install_fake_send(service, handler, []))

    with pytest.raises(MineruTransientIOError, match="-60009"):
        service.parse_pdf(str(source_file), task_id=9, document_id=7)


def test_rag_service_routes_pdf_to_mineru_remote_backend(tmp_path, monkeypatch):
    from backend.services.embed_service import EmbedService
    from backend.services.pdf_parse_types import PDFParseResult
    from backend.services.rag_service import RagService
    from backend.services.vector_store_service import VectorStoreService

    settings = Settings(
        CHROMADB_MODE="persistent",
        CHROMADB_PATH=str(tmp_path / "chromadb"),
        CHROMADB_COLLECTION_PREFIX="studyagent-test",
        TASK_ARTIFACT_PATH=str(tmp_path / "tasks"),
        UPLOAD_PATH=str(tmp_path / "uploads"),
        EMBEDDING_MODEL_NAME="BAAI/bge-m3",
        EMBEDDING_BACKEND="hash",
        EMBEDDING_DEVICE="cpu",
        EMBEDDING_FALLBACK_TO_HASH=True,
        PDF_PARSER_BACKEND="mineru_remote",
    )
    rag_service = RagService(settings=settings, embedder=EmbedService(settings), vector_store=VectorStoreService(settings, EmbedService(settings)))
    source_file = write_source_pdf(tmp_path)

    calls: list[tuple[str, int, int]] = []

    def fake_parse(file_path: str, *, task_id: int, document_id: int):
        calls.append((file_path, task_id, document_id))
        return PDFParseResult(
            text="remote text",
            parser_backend="mineru-remote-official",
            parser_provenance={"provider": "official"},
        )

    monkeypatch.setattr("backend.services.rag_service.mineru_remote_service.parse_pdf", fake_parse)
    monkeypatch.setattr(
        "backend.services.rag_service.mineru_service.parse_pdf",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("local mineru path should not run")),
    )
    monkeypatch.setattr(
        rag_service,
        "_extract_pdf_text",
        lambda _: (_ for _ in ()).throw(AssertionError("legacy path should not run")),
    )

    extracted = rag_service.extract_content(str(source_file), "application/pdf", document_id=7, task_id=9)

    assert calls == [(str(source_file), 9, 7)]
    assert extracted.text == "remote text"
    assert extracted.parser_backend == "mineru-remote-official"
    assert extracted.source_format == "pdf"

    snapshot = rag_service.health_snapshot()
    assert snapshot["pdf_parser"]["parser_backend"] == "mineru_remote"


# ---------------------------------------------------------------------------
# Relay staging (MINERU_REMOTE_RELAY_BASE_URL / MINERU_REMOTE_RELAY_TOKEN)
# ---------------------------------------------------------------------------
RELAY_BASE = "https://relay.example.com"


def build_relay_service(tmp_path: Path, **overrides) -> MineruRemoteService:
    kwargs = dict(
        MINERU_REMOTE_PROVIDERS="302_free",
        MINERU_REMOTE_RELAY_BASE_URL=RELAY_BASE,
        MINERU_REMOTE_RELAY_TOKEN="relay-test-token",
    )
    kwargs.update(overrides)
    return build_service(tmp_path, **kwargs)


def relay_success_handler(zip_bytes: bytes):
    def handler(method, url):
        if url == f"{RELAY_BASE}/mineru-relay/upload" and method == "PUT":
            return FakeResponse(json_body={"path": "/mineru-relay/f/uuid-1.pdf", "expires_in": 7200})
        if url.startswith(f"{RELAY_BASE}/mineru-relay/f/") and method == "DELETE":
            return FakeResponse(json_body={"deleted": True})
        if url.endswith("/api/v4/extract/task") and method == "POST":
            return FakeResponse(json_body={"code": 0, "data": {"task_id": "t-302-free"}})
        if "/api/v4/extract/task/t-302-free" in url and method == "GET":
            return FakeResponse(json_body={"code": 0, "data": {"state": "done", "full_zip_url": "https://cdn.example/result.zip"}})
        if url == "https://cdn.example/result.zip":
            return FakeResponse(content=zip_bytes)
        raise AssertionError(f"unexpected request: {method} {url}")

    return handler


def test_relay_upload_success_feeds_302_with_full_public_url(tmp_path, monkeypatch):
    service = build_relay_service(tmp_path)
    source_file = write_source_pdf(tmp_path)
    calls: list = []
    monkeypatch.setattr(service, "_send", install_fake_send(service, relay_success_handler(build_result_zip()), calls))

    result = service.parse_pdf(str(source_file), task_id=9, document_id=7)

    assert result.parser_backend == "mineru-remote-302_free"
    assert result.parser_provenance["stager"] == "relay"

    put_call = next(c for c in calls if c["method"] == "PUT" and c["url"].endswith("/mineru-relay/upload"))
    assert put_call["headers"]["Authorization"] == "Bearer relay-test-token"
    assert put_call["headers"]["X-Filename"] == "demo.pdf"
    assert put_call["has_content"] is True

    create_call = next(c for c in calls if c["url"].endswith("/api/v4/extract/task") and c["method"] == "POST")
    assert create_call["json_body"]["url"] == f"{RELAY_BASE}/mineru-relay/f/uuid-1.pdf"

    snapshot = service.health_snapshot()
    assert snapshot["provider_status"]["302_free"]["configured"] is True
    assert snapshot["provider_status"]["302_free"]["stager"] == "relay"


def test_relay_file_deleted_after_task_done(tmp_path, monkeypatch):
    service = build_relay_service(tmp_path)
    source_file = write_source_pdf(tmp_path)
    calls: list = []
    monkeypatch.setattr(service, "_send", install_fake_send(service, relay_success_handler(build_result_zip()), calls))

    service.parse_pdf(str(source_file), task_id=9, document_id=7)

    delete_calls = [c for c in calls if c["method"] == "DELETE"]
    assert len(delete_calls) == 1
    assert delete_calls[0]["url"] == f"{RELAY_BASE}/mineru-relay/f/uuid-1.pdf"
    assert delete_calls[0]["headers"]["Authorization"] == "Bearer relay-test-token"


def test_relay_file_deleted_when_remote_task_fails(tmp_path, monkeypatch):
    service = build_relay_service(tmp_path)
    source_file = write_source_pdf(tmp_path)
    calls: list = []

    def handler(method, url):
        if method == "PUT":
            return FakeResponse(json_body={"path": "/mineru-relay/f/uuid-1.pdf"})
        if method == "DELETE":
            return FakeResponse(json_body={"deleted": True})
        if url.endswith("/api/v4/extract/task") and method == "POST":
            return FakeResponse(json_body={"code": 0, "data": {"task_id": "t-302-free"}})
        if method == "GET":
            return FakeResponse(json_body={"code": 0, "data": {"state": "failed", "err_msg": "bad pdf"}})
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(service, "_send", install_fake_send(service, handler, calls))

    with pytest.raises(MineruStartupError, match="bad pdf"):
        service.parse_pdf(str(source_file), task_id=9, document_id=7)
    assert any(c["method"] == "DELETE" for c in calls)


def test_relay_upload_401_raises_startup_error_without_public_files_fallback(tmp_path, monkeypatch):
    service = build_relay_service(tmp_path, MINERU_REMOTE_PUBLIC_BASE_URL="http://files.lan/static")
    source_file = write_source_pdf(tmp_path)
    calls: list = []

    def handler(method, url):
        if method == "PUT":
            return FakeResponse(status_code=401)
        raise AssertionError("302 provider must not be attempted after relay auth failure")

    monkeypatch.setattr(service, "_send", install_fake_send(service, handler, calls))

    with pytest.raises(MineruStartupError, match="401"):
        service.parse_pdf(str(source_file), task_id=9, document_id=7)
    # 未静默落到 public_files：不产生本地副本，也未向 302 发请求
    assert not (tmp_path / "tasks" / "public_files").exists()
    assert all(c["method"] == "PUT" for c in calls)


def test_relay_upload_network_failure_is_transient_and_degrades(tmp_path, monkeypatch):
    service = build_relay_service(tmp_path)
    source_file = write_source_pdf(tmp_path)
    calls: list = []

    def handler(method, url):
        return FakeResponse(status_code=502)

    monkeypatch.setattr(service, "_send", install_fake_send(service, handler, calls))

    with pytest.raises(MineruTransientIOError, match="All MinerU remote providers failed"):
        service.parse_pdf(str(source_file), task_id=9, document_id=7)
    # retried once → 2 relay upload attempts, no 302 request
    assert len(calls) == 2
    assert all(c["url"].endswith("/mineru-relay/upload") for c in calls)


def test_relay_preferred_over_public_base_url(tmp_path, monkeypatch):
    service = build_relay_service(tmp_path, MINERU_REMOTE_PUBLIC_BASE_URL="http://files.lan/static")
    source_file = write_source_pdf(tmp_path)
    calls: list = []
    monkeypatch.setattr(service, "_send", install_fake_send(service, relay_success_handler(build_result_zip()), calls))

    result = service.parse_pdf(str(source_file), task_id=9, document_id=7)

    create_call = next(c for c in calls if c["url"].endswith("/api/v4/extract/task") and c["method"] == "POST")
    assert create_call["json_body"]["url"].startswith(RELAY_BASE)
    assert result.parser_provenance["stager"] == "relay"
    # public_files 路径未被使用
    assert not (tmp_path / "tasks" / "public_files").exists()
