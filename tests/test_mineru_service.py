from pathlib import Path

import pytest
from PIL import Image

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


def test_chat_image_pdf_uses_high_resolution_for_ocr(tmp_path, monkeypatch):
    service = build_service(tmp_path)
    source_file = tmp_path / "question.png"
    Image.new("RGB", (32, 32), color="white").save(source_file)
    captured: dict[str, object] = {}

    def fake_save(self, target, file_format=None, **kwargs):
        captured["target"] = target
        captured["file_format"] = file_format
        captured["resolution"] = kwargs.get("resolution")

    monkeypatch.setattr(Image.Image, "save", fake_save)

    service._write_single_image_pdf(source_file, tmp_path / "question.pdf")

    assert captured["file_format"] == "PDF"
    assert captured["resolution"] == 300.0


def test_flatten_content_repairs_split_office_textstyle_runs_and_keeps_formula_markers(tmp_path):
    service = build_service(tmp_path)

    content = [
        {"type": "text", "content": "7．如图<<text st"},
        {"type": "text", "content": "y", "style": ["italic"]},
        {"type": "text", "content": 'le="italic">t</text>e'},
        {"type": "text", "content": "x", "style": ["italic"]},
        {"type": "text", "content": 't style="italic">xoy</text>平面'},
        {"type": "equation_inline", "content": r"\frac{\pi}{d}"},
        {"type": "text", "content": '与<text style="italic">MNPQ</text>线框'},
    ]

    flattened = service._flatten_content(content)

    assert "<<text" not in flattened
    assert "</text>" not in flattened
    assert 'style="italic"' not in flattened
    assert "xoy平面" in flattened
    assert "MNPQ线框" in flattened
    assert "equation_inline" in flattened
    assert r"\frac{\pi}{d}" in flattened
