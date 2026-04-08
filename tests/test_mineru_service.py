from pathlib import Path

import pytest

from backend.config import Settings
from backend.services.mineru_service import MineruGpuPreflightError, MineruGpuRuntimeError, MineruService


def build_service(tmp_path: Path) -> MineruService:
    settings = Settings(
        PDF_PARSER_BACKEND="mineru",
        TASK_ARTIFACT_PATH=str(tmp_path / "tasks"),
        MINERU_PYTHON_BIN="python3",
        MINERU_BACKEND="pipeline",
        MINERU_PARSE_METHOD="auto",
        MINERU_LANG="ch",
        MINERU_DEVICE="cuda",
        MINERU_DEVICE_MODE="cuda",
        MINERU_MODEL_SOURCE="local",
        MINERU_REQUIRE_GPU_PROOF=True,
    )
    return MineruService(settings=settings)


def test_parse_pdf_fails_closed_when_cuda_preflight_is_not_ready(tmp_path, monkeypatch):
    service = build_service(tmp_path)
    source_file = tmp_path / "demo.pdf"
    source_file.write_bytes(b"%PDF-1.4")

    monkeypatch.setattr(
        service,
        "_collect_cuda_requirement_snapshot",
        lambda requested_device: {
            "ready": False,
            "python": {"ok": True},
            "mineru": {"installed": True, "error": None},
            "torch": {"cuda_available": False, "error": None},
            "nvidia_smi": {"ok": True, "error": None},
        },
    )

    monkeypatch.setattr(service, "_run_parse_command", lambda *args, **kwargs: pytest.fail("parse command should not run"))

    with pytest.raises(MineruGpuPreflightError, match="Torch 未检测到可用 CUDA"):
        service.parse_pdf(str(source_file), task_id=11, document_id=22)


def test_parse_pdf_fails_closed_when_cuda_runtime_drops(tmp_path, monkeypatch):
    service = build_service(tmp_path)
    source_file = tmp_path / "demo.pdf"
    source_file.write_bytes(b"%PDF-1.4")

    monkeypatch.setattr(
        service,
        "_collect_cuda_requirement_snapshot",
        lambda requested_device: {
            "ready": True,
            "python": {"ok": True},
            "mineru": {"installed": True, "error": None},
            "torch": {"cuda_available": True, "error": None},
            "nvidia_smi": {"ok": True, "error": None},
        },
    )

    calls: list[str] = []

    def fake_run(command, env, runtime_artifact, *, runtime_device, requested_device):
        calls.append(runtime_device)
        if runtime_device == "cuda":
            return {
                "returncode": 1,
                "stdout": "",
                "stderr": "RuntimeError: No CUDA GPUs are available",
                "gpu_samples": [],
                "baseline_samples": [],
                "started_at": 1.0,
                "ended_at": 2.0,
            }

    monkeypatch.setattr(service, "_run_parse_command", fake_run)

    with pytest.raises(MineruGpuRuntimeError, match="No CUDA GPUs are available"):
        service.parse_pdf(str(source_file), task_id=12, document_id=23)

    assert calls == ["cuda"]
