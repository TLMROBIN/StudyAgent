from __future__ import annotations

from contextlib import suppress
import json
import mimetypes
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Any

from backend.config import Settings, get_settings
from backend.services.gpu_runtime import collect_cuda_requirement_snapshot
from backend.services.pdf_parse_types import ExtractedAsset, PDFBlock, PDFParseResult


class MineruError(RuntimeError):
    pass


class MineruStartupError(MineruError):
    pass


class MineruTransientIOError(MineruError):
    pass


class MineruTimeoutError(MineruError):
    pass


class MineruGpuPreflightError(MineruError):
    pass


class MineruGpuRuntimeError(MineruError):
    pass


class GPUProofFailedError(MineruError):
    pass


class MineruMalformedOutputError(MineruError):
    pass


class UnsupportedPdfError(MineruError):
    pass


_PARSE_SCRIPT = r'''
import sys
from pathlib import Path
from mineru.cli.common import do_parse

pdf_path = Path(sys.argv[1])
out_dir = Path(sys.argv[2])
backend = sys.argv[3]
parse_method = sys.argv[4]
lang = sys.argv[5]
start_page = int(sys.argv[6])
end_page_raw = sys.argv[7]
end_page = None if end_page_raw == "none" else int(end_page_raw)
out_dir.mkdir(parents=True, exist_ok=True)
do_parse(
    str(out_dir),
    [pdf_path.name],
    [pdf_path.read_bytes()],
    [lang],
    backend=backend,
    parse_method=parse_method,
    start_page_id=start_page,
    end_page_id=end_page,
)
print("OK")
'''


class MineruService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def parse_smoke(self, file_path: str, *, task_id: int, document_id: int) -> PDFParseResult:
        return self.parse_pdf(file_path, task_id=task_id, document_id=document_id, end_page=0)

    def parse_pdf(
        self,
        file_path: str,
        *,
        task_id: int,
        document_id: int,
        start_page: int = 0,
        end_page: int | None = None,
    ) -> PDFParseResult:
        if self.settings.pdf_parser_backend != "mineru":
            raise MineruStartupError("MinerU PDF parser backend is not enabled")

        task_dir = Path(self.settings.task_artifact_path) / str(task_id)
        mineru_dir = task_dir / "mineru"
        runtime_artifact = task_dir / "mineru-runtime.json"
        output_root = mineru_dir / "output"
        shutil.rmtree(mineru_dir, ignore_errors=True)
        output_root.mkdir(parents=True, exist_ok=True)

        requested_device = self.settings.mineru_device
        runtime_device = self._resolve_runtime_device(requested_device)

        command = [
            self.settings.mineru_python_bin,
            "-c",
            _PARSE_SCRIPT,
            file_path,
            str(output_root),
            self.settings.mineru_backend,
            self.settings.mineru_parse_method,
            self.settings.mineru_lang,
            str(start_page),
            "none" if end_page is None else str(end_page),
        ]
        env = self._build_runtime_env(runtime_device)

        run = self._run_parse_command(command, env, runtime_artifact, runtime_device=runtime_device, requested_device=requested_device)

        stdout = run["stdout"]
        stderr = run["stderr"]
        ended_at = run["ended_at"]

        if run["returncode"] != 0:
            if runtime_device == "cuda" and self._is_cuda_unavailable_error(stderr):
                raise MineruGpuRuntimeError(stderr.strip() or "MinerU CUDA runtime is unavailable")
            lowered = (stderr or "").lower()
            if "permission denied" in lowered or "operation not permitted" in lowered:
                raise MineruTransientIOError(stderr.strip() or "MinerU runtime IO failed")
            raise MineruStartupError(stderr.strip() or stdout.strip() or "MinerU parse failed")

        if self.settings.mineru_require_gpu_proof and runtime_device == "cuda":
            if not self._gpu_proof_passed(run["gpu_samples"], run["baseline_samples"]):
                raise GPUProofFailedError("MinerU parse did not produce GPU proof during real ingest")

        try:
            content_list_path = next(output_root.rglob("*_content_list_v2.json"))
        except StopIteration as exc:
            raise MineruMalformedOutputError("MinerU did not produce content_list_v2 output") from exc

        try:
            data = json.loads(content_list_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MineruMalformedOutputError("MinerU content_list_v2 output is invalid JSON") from exc

        if not isinstance(data, list):
            raise MineruMalformedOutputError("MinerU content_list_v2 output must be a page list")

        document_asset_dir = Path(self.settings.task_artifact_path) / "knowledge" / str(document_id)
        shutil.rmtree(document_asset_dir, ignore_errors=True)
        document_asset_dir.mkdir(parents=True, exist_ok=True)

        image_paths = sorted({self._extract_image_path(item) for page in data for item in page if self._extract_image_path(item)})
        asset_lookup: dict[str, ExtractedAsset] = {}
        assets: list[ExtractedAsset] = []
        for index, relative_path in enumerate(image_paths, start=1):
            source = content_list_path.parent / relative_path
            if not source.is_file():
                continue
            suffix = source.suffix.lower() or ".bin"
            asset_id = f"image-{index:03d}"
            filename = f"{asset_id}{suffix}"
            target = document_asset_dir / filename
            shutil.copy2(source, target)
            asset = ExtractedAsset(
                asset_id=asset_id,
                filename=filename,
                content_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
                storage_path=str(target),
                public_url=f"/api/knowledge/documents/{document_id}/assets/{filename}",
                title=source.stem,
                description=relative_path,
            )
            asset_lookup[relative_path] = asset
            assets.append(asset)

        blocks: list[PDFBlock] = []
        text_parts: list[str] = []
        for page_index, page in enumerate(data):
            if not isinstance(page, list):
                raise MineruMalformedOutputError("Each MinerU page entry must be a list")
            for item in page:
                block = self._normalize_block(item, page_index, asset_lookup)
                if block is None:
                    continue
                blocks.append(block)
                if block.text.strip():
                    text_parts.append(block.text.strip())

        return PDFParseResult(
            text="\n\n".join(text_parts).strip(),
            blocks=blocks,
            assets=assets,
            parser_backend=self.settings.mineru_backend,
            parser_provenance={
                "task_id": task_id,
                "runtime_artifact": str(runtime_artifact),
                "requested_device": requested_device,
                "effective_device": runtime_device,
                "device": runtime_device,
                "parse_seconds": round(ended_at - run["started_at"], 2),
                "warn_threshold_seconds": self.settings.mineru_parse_warn_seconds,
                "content_list_path": str(content_list_path),
            },
        )

    def _normalize_block(
        self,
        item: dict[str, Any],
        page_index: int,
        asset_lookup: dict[str, ExtractedAsset],
    ) -> PDFBlock | None:
        if not isinstance(item, dict):
            return None
        block_type = str(item.get("type") or "paragraph")
        content = item.get("content") or {}
        text = self._flatten_content(content)
        asset_id = None
        image_path = self._extract_image_path(item)
        if image_path and image_path in asset_lookup:
            asset = asset_lookup[image_path]
            asset_id = asset.asset_id
            marker = f"[[asset:{asset.asset_id}]]"
            text = f"{marker}\n{text}".strip() if text else marker
        level = None
        if isinstance(content, dict):
            title_content = content.get("title_content") or {}
            if isinstance(title_content, dict):
                level = title_content.get("level")
            level = level or content.get("level")
        return PDFBlock(
            page_index=page_index,
            block_type=block_type,
            text=text.strip(),
            level=int(level) if isinstance(level, int) else None,
            asset_id=asset_id,
            metadata={"raw_type": block_type},
        )

    def _flatten_content(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            parts = [self._flatten_content(item) for item in value]
            return "\n".join(part for part in parts if part).strip()
        if isinstance(value, dict):
            if value.get("type") == "text":
                return str(value.get("content") or "").strip()
            ordered_keys = [
                "title_content",
                "paragraph_content",
                "table_caption",
                "table_body",
                "table_footnote",
                "image_caption",
                "image_footnote",
                "algorithm_caption",
                "algorithm_content",
                "algorithm_footnote",
                "list_items",
                "page_footer_content",
            ]
            parts = [self._flatten_content(value.get(key)) for key in ordered_keys if key in value]
            if not any(parts):
                parts = [self._flatten_content(item) for item in value.values()]
            return "\n".join(part for part in parts if part).strip()
        return str(value).strip()

    def _extract_image_path(self, item: dict[str, Any]) -> str | None:
        content = item.get("content") or {}
        image_source = content.get("image_source") if isinstance(content, dict) else None
        if isinstance(image_source, dict):
            path = image_source.get("path")
            if path:
                return str(path)
        return None

    def _build_runtime_env(self, runtime_device: str) -> dict[str, str]:
        env = os.environ.copy()
        env["MINERU_MODEL_SOURCE"] = self.settings.mineru_model_source
        env["MINERU_DEVICE_MODE"] = runtime_device
        if runtime_device == "cuda":
            env["CUDA_VISIBLE_DEVICES"] = env.get("CUDA_VISIBLE_DEVICES", "0")
        else:
            env.pop("CUDA_VISIBLE_DEVICES", None)
        return env

    def _resolve_runtime_device(self, requested_device: str) -> str:
        if requested_device != "cuda":
            return requested_device
        snapshot = self._collect_cuda_requirement_snapshot(requested_device)
        if snapshot.get("ready"):
            return "cuda"
        raise MineruGpuPreflightError(self._format_cuda_requirement_error(snapshot))

    def _collect_cuda_requirement_snapshot(self, requested_device: str) -> dict[str, Any]:
        return collect_cuda_requirement_snapshot(requested_device, python_bin=self.settings.mineru_python_bin)

    def _format_cuda_requirement_error(self, snapshot: dict[str, Any]) -> str:
        reasons: list[str] = []
        python_ok = bool(snapshot.get("python", {}).get("ok", True))
        mineru_installed = bool(snapshot.get("mineru", {}).get("installed"))
        torch_cuda = bool(snapshot.get("torch", {}).get("cuda_available"))
        nvidia_smi_ok = bool(snapshot.get("nvidia_smi", {}).get("ok"))

        if not python_ok:
            reasons.append("MinerU Python 运行环境不可用")
        if not mineru_installed:
            reasons.append("MinerU 未安装")
        if not torch_cuda:
            reasons.append("Torch 未检测到可用 CUDA")
        if not nvidia_smi_ok:
            reasons.append("nvidia-smi 不可用")

        detail = (
            snapshot.get("nvidia_smi", {}).get("error")
            or snapshot.get("torch", {}).get("error")
            or snapshot.get("mineru", {}).get("error")
        )
        reason_text = "；".join(reasons) if reasons else "GPU 运行环境未就绪"
        if detail:
            return f"{reason_text}（{detail}）"
        return reason_text

    def _run_parse_command(
        self,
        command: list[str],
        env: dict[str, str],
        runtime_artifact: Path,
        *,
        runtime_device: str,
        requested_device: str,
    ) -> dict[str, Any]:
        gpu_samples: list[dict[str, Any]] = []
        baseline_samples = self._sample_gpu_processes()
        started_at = time.time()
        proc: subprocess.Popen[str] | None = None

        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
        except FileNotFoundError as exc:
            raise MineruStartupError(f"MinerU python binary not found: {self.settings.mineru_python_bin}") from exc
        except OSError as exc:
            raise MineruStartupError(f"Failed to start MinerU runtime: {exc}") from exc

        stop_event = threading.Event()
        sampler = threading.Thread(target=self._poll_gpu_usage, args=(proc.pid, gpu_samples, stop_event), daemon=True)
        sampler.start()

        try:
            stdout, stderr = proc.communicate(timeout=self.settings.mineru_parse_timeout_seconds)
        except subprocess.TimeoutExpired:
            stop_event.set()
            with suppress(Exception):
                proc.kill()
                proc.communicate(timeout=5)
            ended_at = time.time()
            self._write_runtime_artifact(
                runtime_artifact,
                proc.pid,
                started_at,
                ended_at,
                gpu_samples,
                baseline_samples,
                requested_device=requested_device,
                effective_device=runtime_device,
                stdout="",
                stderr="timeout",
            )
            raise MineruTimeoutError(
                f"MinerU parse exceeded {self.settings.mineru_parse_timeout_seconds}s timeout"
            )
        finally:
            stop_event.set()
            sampler.join(timeout=2)

        ended_at = time.time()
        self._write_runtime_artifact(
            runtime_artifact,
            proc.pid,
            started_at,
            ended_at,
            gpu_samples,
            baseline_samples,
            requested_device=requested_device,
            effective_device=runtime_device,
            stdout=stdout,
            stderr=stderr,
        )
        return {
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "gpu_samples": gpu_samples,
            "baseline_samples": baseline_samples,
            "started_at": started_at,
            "ended_at": ended_at,
        }

    def _is_cuda_unavailable_error(self, stderr: str) -> bool:
        lowered = (stderr or "").lower()
        return "no cuda gpus are available" in lowered or "failed to initialize nvml" in lowered

    def _poll_gpu_usage(self, pid: int, samples: list[dict[str, Any]], stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            samples.extend(self._sample_gpu_processes(target_pid=pid))
            stop_event.wait(1.0)

    def _sample_gpu_processes(self, target_pid: int | None = None) -> list[dict[str, Any]]:
        command = [
            "nvidia-smi",
            "--query-compute-apps=pid,process_name,used_gpu_memory",
            "--format=csv,noheader,nounits",
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
        except Exception:
            return []
        if result.returncode != 0:
            return []
        rows: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 3:
                continue
            try:
                pid = int(parts[0])
                used_gpu_memory = int(parts[2])
            except ValueError:
                continue
            if target_pid is not None and pid != target_pid:
                continue
            rows.append({"timestamp": time.time(), "pid": pid, "process_name": parts[1], "used_gpu_memory_mb": used_gpu_memory})
        return rows

    def _gpu_proof_passed(self, samples: list[dict[str, Any]], baseline: list[dict[str, Any]]) -> bool:
        baseline_pids = {sample.get("pid") for sample in baseline}
        for sample in samples:
            if sample.get("used_gpu_memory_mb", 0) > 0 and sample.get("pid") not in baseline_pids:
                return True
        return False

    def _write_runtime_artifact(
        self,
        path: Path,
        pid: int,
        started_at: float,
        ended_at: float,
        gpu_samples: list[dict[str, Any]],
        baseline_samples: list[dict[str, Any]],
        *,
        requested_device: str,
        effective_device: str,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        payload = {
            "parser_backend": self.settings.mineru_backend,
            "requested_device": requested_device,
            "effective_device": effective_device,
            "selected_device": effective_device,
            "pid": pid,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_seconds": round(max(ended_at - started_at, 0), 2),
            "baseline_gpu_samples": baseline_samples,
            "gpu_samples": gpu_samples,
            "gpu_proof_passed": self._gpu_proof_passed(gpu_samples, baseline_samples),
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def health_snapshot(self) -> dict[str, Any]:
        enabled = self.settings.pdf_parser_backend == "mineru"
        gpu_snapshot = collect_cuda_requirement_snapshot(
            self.settings.mineru_device if enabled else None,
            python_bin=self.settings.mineru_python_bin if enabled else None,
        )
        return {
            "enabled": enabled,
            "configured_backend": self.settings.pdf_parser_backend,
            "parser_backend": self.settings.mineru_backend,
            "configured_device": self.settings.mineru_device,
            "python_bin": self.settings.mineru_python_bin,
            "require_gpu_proof": self.settings.mineru_require_gpu_proof,
            "runtime_ready": gpu_snapshot["runtime_ready"] if enabled else True,
            "mineru": gpu_snapshot.get("mineru", {}),
            "gpu": gpu_snapshot,
        }


mineru_service = MineruService()
