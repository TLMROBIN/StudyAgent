"""MinerU remote HTTP API parse backend (PDF_PARSER_BACKEND="mineru_remote").

Provider degradation chain (configurable via MINERU_REMOTE_PROVIDERS):

1. ``official``   — MinerU official cloud API (https://mineru.net, /api/v4).
                     Supports direct local-file upload via OSS file-urls.
2. ``302_free``   — 302.ai hosted MinerU v4 API. Only accepts a public file
                     URL (no upload), so it requires MINERU_REMOTE_PUBLIC_BASE_URL
                     pointing at a file-serving endpoint that can already reach
                     files under ``task_artifact_path`` (e.g. a static server the
                     operator runs themselves). Not configured → provider skipped.
3. ``302_paid``   — 302.ai paid MinerU API (/302/v2/mineru/task). Also URL-only.

Degradation happens ONLY on network-class failures (connect errors, timeouts,
HTTP 5xx, 429, quota/limit error codes such as -60009 / -60018). Auth failures
(401/403, code A0211) and remote parse failures (state=failed) do NOT degrade —
they raise immediately.

Interface mirrors ``MineruService``: parse_pdf / parse_docx / parse_smoke /
ocr_image_via_pdf / health_snapshot, returning PDFParseResult.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import shutil
import time
from typing import Any
import zipfile

import httpx

from backend.config import Settings, get_settings
from backend.services.mineru_content_list import assemble_content_list_result
from backend.services.mineru_service import (
    MineruMalformedOutputError,
    MineruService,
    MineruStartupError,
    MineruTransientIOError,
)
from backend.services.pdf_parse_types import PDFParseResult


# Quota / rate-limit error codes returned by the MinerU v4 API that count as
# transient (degrade to the next provider after one retry).
_TRANSIENT_API_CODES = {-60009, -60018}
# Auth-class API error codes — never transient, never degrade.
_AUTH_API_CODES = {"A0211"}


class _ProviderSkipped(RuntimeError):
    """Internal signal: provider cannot run due to missing optional config."""


class MineruRemoteService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        # Reused only for the single-image-PDF helper in ocr_image_via_pdf.
        self._local_service = MineruService(settings=self.settings)

    # ------------------------------------------------------------------
    # Public interface (mirrors MineruService)
    # ------------------------------------------------------------------
    def parse_smoke(self, file_path: str, *, task_id: int, document_id: int) -> PDFParseResult:
        return self.parse_pdf(file_path, task_id=task_id, document_id=document_id, end_page=0)

    def parse_docx(
        self,
        file_path: str,
        *,
        task_id: int,
        document_id: int,
    ) -> PDFParseResult:
        return self._run_chain(
            file_path,
            task_id=task_id,
            document_id=document_id,
            start_page=0,
            end_page=None,
            timeout_seconds=None,
        )

    def parse_pdf(
        self,
        file_path: str,
        *,
        task_id: int,
        document_id: int,
        start_page: int = 0,
        end_page: int | None = None,
        timeout_seconds: int | None = None,
    ) -> PDFParseResult:
        if self.settings.pdf_parser_backend != "mineru_remote":
            raise MineruStartupError("MinerU remote PDF parser backend is not enabled")
        return self._run_chain(
            file_path,
            task_id=task_id,
            document_id=document_id,
            start_page=start_page,
            end_page=end_page,
            timeout_seconds=timeout_seconds,
        )

    def ocr_image_via_pdf(
        self,
        image_path: str,
        *,
        task_id: int,
        document_id: int,
        timeout_seconds: int | None = None,
    ) -> PDFParseResult:
        source = Path(image_path)
        task_dir = Path(self.settings.task_artifact_path) / str(task_id)
        pdf_path = task_dir / "chat-image-ocr-source.pdf"
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        self._local_service._write_single_image_pdf(source, pdf_path)
        return self.parse_pdf(
            str(pdf_path),
            task_id=task_id,
            document_id=-abs(document_id),
            timeout_seconds=timeout_seconds,
        )

    def health_snapshot(self) -> dict[str, Any]:
        enabled = self.settings.pdf_parser_backend == "mineru_remote"
        providers = self._configured_providers()
        provider_status: dict[str, dict[str, Any]] = {}
        for name in providers:
            provider_status[name] = self._provider_status(name)
        return {
            "enabled": enabled,
            "configured_backend": self.settings.pdf_parser_backend,
            "parser_backend": "mineru_remote",
            "providers": providers,
            "provider_status": provider_status,
            "model_version": self.settings.mineru_remote_model_version,
            "poll_interval_seconds": self.settings.mineru_remote_poll_interval_seconds,
            "timeout_seconds": self.settings.mineru_remote_timeout_seconds,
            "runtime_ready": enabled,
        }

    # ------------------------------------------------------------------
    # Provider chain
    # ------------------------------------------------------------------
    def _configured_providers(self) -> list[str]:
        return [
            name.strip()
            for name in str(self.settings.mineru_remote_providers or "").split(",")
            if name.strip()
        ]

    def _run_chain(
        self,
        file_path: str,
        *,
        task_id: int,
        document_id: int,
        start_page: int,
        end_page: int | None,
        timeout_seconds: int | None,
    ) -> PDFParseResult:
        providers = self._configured_providers()
        if not providers:
            raise MineruStartupError("MINERU_REMOTE_PROVIDERS is empty")

        attempts: list[dict[str, Any]] = []
        for name in providers:
            runner = {
                "official": self._run_official,
                "302_free": self._run_302_free,
                "302_paid": self._run_302_paid,
            }.get(name)
            if runner is None:
                attempts.append({"provider": name, "status": "skipped", "reason": "unknown provider"})
                continue
            try:
                zip_url, zip_headers = self._call_with_retry(
                    lambda runner=runner: runner(file_path, task_id=task_id, timeout_seconds=timeout_seconds)
                )
            except _ProviderSkipped as exc:
                attempts.append({"provider": name, "status": "skipped", "reason": str(exc)})
                continue
            except MineruTransientIOError as exc:
                # Network-class failure → degrade to next provider.
                attempts.append({"provider": name, "status": "failed", "reason": str(exc)})
                continue
            # MineruStartupError (auth / remote parse failure) propagates: no degrade.
            attempts.append({"provider": name, "status": "ok"})
            return self._download_and_assemble(
                zip_url,
                provider=name,
                task_id=task_id,
                document_id=document_id,
                file_path=file_path,
                start_page=start_page,
                end_page=end_page,
                attempts=attempts,
                headers=zip_headers,
            )

        detail = "; ".join(f"{a['provider']}: {a.get('reason', 'ok')}" for a in attempts) or "no providers attempted"
        raise MineruTransientIOError(f"All MinerU remote providers failed ({detail})")

    def _call_with_retry(self, fn):
        """Retry a provider once on transient (network-class) failure."""
        try:
            return fn()
        except MineruTransientIOError:
            return fn()

    # ------------------------------------------------------------------
    # Providers — each returns (full_zip_url, headers_for_zip_download)
    # ------------------------------------------------------------------
    def _run_official(
        self,
        file_path: str,
        *,
        task_id: int,
        timeout_seconds: int | None,
    ) -> tuple[str, dict[str, str] | None]:
        api_key = self.settings.mineru_remote_api_key
        if not api_key:
            raise MineruStartupError("MINERU_REMOTE_API_KEY is not configured")
        base = self.settings.mineru_remote_api_base.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"}

        payload = {
            "files": [{"name": Path(file_path).name, "is_ocr": True}],
            "model_version": self.settings.mineru_remote_model_version,
            "enable_formula": True,
            "enable_table": True,
            "language": self.settings.mineru_lang,
        }
        body = self._request_json("POST", f"{base}/api/v4/file-urls/batch", headers=headers, json_body=payload)
        data = body.get("data") or {}
        batch_id = data.get("batch_id")
        file_urls = data.get("file_urls") or []
        upload_url = file_urls[0] if file_urls else None
        if not batch_id or not upload_url:
            raise MineruStartupError(f"MinerU official API returned no batch/upload url: {body!r}")

        # OSS upload: raw binary PUT, no Authorization, no Content-Type.
        self._send("PUT", upload_url, content=Path(file_path).read_bytes())

        deadline = self._deadline(timeout_seconds)
        while True:
            body = self._request_json("GET", f"{base}/api/v4/extract-results/batch/{batch_id}", headers=headers)
            results = (body.get("data") or {}).get("extract_result") or []
            states = {str(item.get("state") or "") for item in results}
            if "failed" in states:
                failed = next((item for item in results if item.get("state") == "failed"), {})
                reason = failed.get("err_msg") or failed.get("reason") or "state=failed"
                raise MineruStartupError(f"MinerU official remote parse failed: {reason}")
            if results and states == {"done"}:
                zip_url = results[0].get("full_zip_url")
                if not zip_url:
                    raise MineruStartupError("MinerU official API returned done without full_zip_url")
                return str(zip_url), headers
            self._sleep_or_timeout(deadline)

    def _run_302_free(
        self,
        file_path: str,
        *,
        task_id: int,
        timeout_seconds: int | None,
    ) -> tuple[str, dict[str, str] | None]:
        api_key = self.settings.mineru_remote_302_api_key
        if not api_key:
            raise MineruStartupError("MINERU_REMOTE_302_API_KEY is not configured")
        file_url = self._public_file_url(file_path, task_id=task_id)
        base = self.settings.mineru_remote_302_base.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"}

        payload = {
            "url": file_url,
            "model_version": self.settings.mineru_remote_model_version,
            "enable_formula": True,
            "enable_table": True,
            "language": self.settings.mineru_lang,
        }
        body = self._request_json("POST", f"{base}/api/v4/extract/task", headers=headers, json_body=payload)
        task_ref = (body.get("data") or {}).get("task_id") or body.get("task_id")
        if not task_ref:
            raise MineruStartupError(f"302.ai free API returned no task_id: {body!r}")

        deadline = self._deadline(timeout_seconds)
        while True:
            body = self._request_json("GET", f"{base}/api/v4/extract/task/{task_ref}", headers=headers)
            data = body.get("data") or {}
            state = str(data.get("state") or "")
            if state == "failed":
                raise MineruStartupError(f"302.ai free remote parse failed: {data.get('err_msg') or 'state=failed'}")
            if state == "done":
                zip_url = data.get("full_zip_url")
                if not zip_url:
                    raise MineruStartupError("302.ai free API returned done without full_zip_url")
                return str(zip_url), headers
            self._sleep_or_timeout(deadline)

    def _run_302_paid(
        self,
        file_path: str,
        *,
        task_id: int,
        timeout_seconds: int | None,
    ) -> tuple[str, dict[str, str] | None]:
        api_key = self.settings.mineru_remote_302_api_key
        if not api_key:
            raise MineruStartupError("MINERU_REMOTE_302_API_KEY is not configured")
        file_url = self._public_file_url(file_path, task_id=task_id)
        base = self.settings.mineru_remote_302_base.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"}

        payload = {"pdf_url": file_url, "parse_method": "auto", "version": "2.5"}
        body = self._request_json("POST", f"{base}/302/v2/mineru/task", headers=headers, json_body=payload)
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        task_ref = (data or {}).get("task_id")
        if not task_ref:
            raise MineruStartupError(f"302.ai paid API returned no task_id: {body!r}")

        deadline = self._deadline(timeout_seconds)
        while True:
            body = self._request_json(
                "GET",
                f"{base}/302/v2/mineru/task",
                headers=headers,
                params={"task_id": task_ref},
            )
            data = body.get("data") if isinstance(body.get("data"), dict) else body
            data = data or {}
            state = str(data.get("state") or data.get("status") or "")
            if state == "failed":
                raise MineruStartupError(f"302.ai paid remote parse failed: {data.get('err_msg') or 'state=failed'}")
            if state in {"done", "success"}:
                zip_url = data.get("full_zip_url") or data.get("zip_url")
                if not zip_url:
                    raise MineruStartupError("302.ai paid API returned done without zip url")
                return str(zip_url), headers
            self._sleep_or_timeout(deadline)

    # ------------------------------------------------------------------
    # Result zip download / unpack / normalize
    # ------------------------------------------------------------------
    def _download_and_assemble(
        self,
        zip_url: str,
        *,
        provider: str,
        task_id: int,
        document_id: int,
        file_path: str,
        start_page: int,
        end_page: int | None,
        attempts: list[dict[str, Any]],
        headers: dict[str, str] | None,
    ) -> PDFParseResult:
        started_at = time.time()
        response = self._send("GET", zip_url, headers=headers)

        remote_dir = Path(self.settings.task_artifact_path) / str(task_id) / "mineru_remote"
        output_root = remote_dir / "output"
        shutil.rmtree(remote_dir, ignore_errors=True)
        output_root.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                archive.extractall(output_root)
        except zipfile.BadZipFile as exc:
            raise MineruMalformedOutputError(f"MinerU remote result is not a valid zip: {zip_url}") from exc

        content_list_path, data = self._load_content_list(output_root)

        document_asset_dir = Path(self.settings.task_artifact_path) / "knowledge" / str(document_id)
        shutil.rmtree(document_asset_dir, ignore_errors=True)
        document_asset_dir.mkdir(parents=True, exist_ok=True)

        # Image paths inside the content_list are usually relative to the JSON's
        # own directory; some zips place them relative to the zip root instead.
        content_list_dir = content_list_path.parent
        sample_image = next(
            (
                item.get("content", {}).get("image_source", {}).get("path")
                for page in data
                for item in page
                if isinstance(item, dict)
                and isinstance(item.get("content"), dict)
                and isinstance(item["content"].get("image_source"), dict)
                and item["content"]["image_source"].get("path")
            ),
            None,
        )
        if sample_image and not (content_list_dir / sample_image).is_file() and (output_root / sample_image).is_file():
            content_list_dir = output_root

        try:
            text, blocks, assets = assemble_content_list_result(
                data,
                content_list_dir=content_list_dir,
                document_asset_dir=document_asset_dir,
                document_id=document_id,
            )
        except ValueError as exc:
            raise MineruMalformedOutputError(str(exc)) from exc

        return PDFParseResult(
            text=text,
            blocks=blocks,
            assets=assets,
            parser_backend=f"mineru-remote-{provider}",
            parser_provenance={
                "task_id": task_id,
                "provider": provider,
                "provider_attempts": attempts,
                "source_format": Path(file_path).suffix.lower().lstrip(".") or None,
                "requested_page_range": [start_page, end_page],
                "page_range_applied": False,  # remote API parses the whole document
                "parse_seconds": round(time.time() - started_at, 2),
                "content_list_path": str(content_list_path),
                "zip_url": zip_url,
            },
        )

    def _load_content_list(self, output_root: Path) -> tuple[Path, list[list[Any]]]:
        """Locate and parse the content_list JSON inside an extracted MinerU zip.

        The official zip layout differs slightly from the local subprocess one:
        prefer files named like ``*content_list*.json``; fall back to any JSON
        that parses into a page list. Both page-list (list of lists) and flat
        item-list (items carrying ``page_idx``/``page``) shapes are accepted.
        """
        candidates = sorted(output_root.rglob("*content_list*.json")) or sorted(output_root.rglob("*.json"))
        malformed: list[str] = []
        for path in candidates:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            pages = self._coerce_page_list(data)
            if pages is not None:
                return path, pages
            malformed.append(path.name)
        raise MineruMalformedOutputError(
            f"MinerU remote zip did not contain a usable content_list JSON (checked: {malformed or 'none found'})"
        )

    @staticmethod
    def _coerce_page_list(data: Any) -> list[list[Any]] | None:
        if not isinstance(data, list) or not data:
            return None
        if all(isinstance(page, list) for page in data):
            return data
        if all(isinstance(item, dict) for item in data):
            grouped: dict[int, list[Any]] = {}
            for item in data:
                page_index = item.get("page_idx", item.get("page", 0))
                try:
                    page_index = int(page_index)
                except (TypeError, ValueError):
                    page_index = 0
                grouped.setdefault(page_index, []).append(item)
            return [grouped[key] for key in sorted(grouped)]
        return None

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self._send(method, url, headers=headers, json_body=json_body, params=params)
        try:
            body = response.json()
        except ValueError as exc:
            raise MineruStartupError(f"MinerU remote API returned non-JSON response from {url}") from exc
        if not isinstance(body, dict):
            raise MineruStartupError(f"MinerU remote API returned unexpected payload from {url}")
        self._raise_for_api_error(body, url)
        return body

    def _raise_for_api_error(self, body: dict[str, Any], url: str) -> None:
        code = body.get("code")
        if code in (None, 0, "0"):
            return
        if code in _TRANSIENT_API_CODES:
            raise MineruTransientIOError(f"MinerU remote API quota/limit error {code} from {url}")
        if code in _AUTH_API_CODES:
            raise MineruStartupError(f"MinerU remote API auth error {code} from {url}")
        raise MineruStartupError(f"MinerU remote API error {code}: {body.get('msg') or body.get('message') or url}")

    def _send(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        """Single low-level HTTP entry point (monkeypatched in tests).

        Classifies failures: connect/timeout/5xx/429 → transient (degradable);
        401/403 and other 4xx → startup/auth error (no degrade).
        """
        try:
            with httpx.Client(timeout=self.settings.mineru_remote_timeout_seconds) as client:
                response = client.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    params=params,
                    content=content,
                )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise MineruTransientIOError(f"MinerU remote request failed ({method} {url}): {exc}") from exc
        except httpx.HTTPError as exc:
            raise MineruTransientIOError(f"MinerU remote request failed ({method} {url}): {exc}") from exc

        return self._classify_response(response, url)

    @staticmethod
    def _classify_response(response, url: str):
        """Classify HTTP status: 401/403 → auth (no degrade); 429/5xx → transient."""
        status = response.status_code
        if status in {401, 403}:
            raise MineruStartupError(f"MinerU remote API auth failed (HTTP {status}) for {url}")
        if status == 429 or status >= 500:
            raise MineruTransientIOError(f"MinerU remote API transient HTTP {status} for {url}")
        if status >= 400:
            raise MineruStartupError(f"MinerU remote API HTTP {status} for {url}")
        return response

    # ------------------------------------------------------------------
    # Misc helpers
    # ------------------------------------------------------------------
    def _public_file_url(self, file_path: str, *, task_id: int) -> str:
        """Expose a local file under MINERU_REMOTE_PUBLIC_BASE_URL for URL-only providers.

        The file is copied to ``task_artifact_path/public_files/`` and the URL is
        built by joining the configured base. The operator must run their own
        static file service that maps that base URL onto this directory — this
        project intentionally does NOT add a new download endpoint for it.
        """
        base_url = str(self.settings.mineru_remote_public_base_url or "").strip().rstrip("/")
        if not base_url:
            raise _ProviderSkipped("MINERU_REMOTE_PUBLIC_BASE_URL is not configured; URL-only provider skipped")
        public_root = Path(self.settings.task_artifact_path) / "public_files"
        public_root.mkdir(parents=True, exist_ok=True)
        target = public_root / f"{task_id}-{Path(file_path).name}"
        shutil.copy2(file_path, target)
        return f"{base_url}/{target.name}"

    def _deadline(self, timeout_seconds: int | None) -> float:
        return time.time() + (timeout_seconds or self.settings.mineru_remote_timeout_seconds)

    def _sleep_or_timeout(self, deadline: float) -> None:
        if time.time() >= deadline:
            raise MineruTransientIOError("MinerU remote polling exceeded timeout")
        time.sleep(self.settings.mineru_remote_poll_interval_seconds)

    def _provider_status(self, name: str) -> dict[str, Any]:
        if name == "official":
            configured = bool(self.settings.mineru_remote_api_key)
            return {
                "configured": configured,
                "skipped_reason": None if configured else "MINERU_REMOTE_API_KEY is not configured",
            }
        if name in {"302_free", "302_paid"}:
            if not self.settings.mineru_remote_302_api_key:
                return {"configured": False, "skipped_reason": "MINERU_REMOTE_302_API_KEY is not configured"}
            if not str(self.settings.mineru_remote_public_base_url or "").strip():
                return {
                    "configured": False,
                    "skipped_reason": "MINERU_REMOTE_PUBLIC_BASE_URL is not configured; provider requires a reachable file URL",
                }
            return {"configured": True, "skipped_reason": None}
        return {"configured": False, "skipped_reason": "unknown provider"}


mineru_remote_service = MineruRemoteService()
