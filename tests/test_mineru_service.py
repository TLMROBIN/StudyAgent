from pathlib import Path

from backend.config import Settings
from backend.services.mineru_service import MineruService
from backend.services.pdf_parse_types import PDFParseResult


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


def write_fake_content_list(base: Path) -> None:
    out_dir = base / "demo"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "demo_content_list_v2.json").write_text(
        '[[{"type":"paragraph","content":"第一章 运动"}]]',
        encoding="utf-8",
    )


def test_parse_pdf_falls_back_to_cpu_when_cuda_probe_fails(tmp_path, monkeypatch):
    service = build_service(tmp_path)
    source_file = tmp_path / "demo.pdf"
    source_file.write_bytes(b"%PDF-1.4")

    monkeypatch.setattr(service, "_cuda_is_available", lambda: False)

    calls: list[str] = []

    def fake_run(command, env, runtime_artifact, *, runtime_device, requested_device):
        calls.append(runtime_device)
        write_fake_content_list(Path(command[4]))
        return {
            "returncode": 0,
            "stdout": "OK",
            "stderr": "",
            "gpu_samples": [],
            "baseline_samples": [],
            "started_at": 1.0,
            "ended_at": 3.5,
        }

    monkeypatch.setattr(service, "_run_parse_command", fake_run)

    result = service.parse_pdf(str(source_file), task_id=11, document_id=22)

    assert isinstance(result, PDFParseResult)
    assert calls == ["cpu"]
    assert result.parser_provenance["requested_device"] == "cuda"
    assert result.parser_provenance["effective_device"] == "cpu"
    assert result.parser_provenance["device_fallback_reason"] == "cuda_unavailable"


def test_parse_pdf_retries_on_cpu_when_cuda_runtime_fails(tmp_path, monkeypatch):
    service = build_service(tmp_path)
    source_file = tmp_path / "demo.pdf"
    source_file.write_bytes(b"%PDF-1.4")

    monkeypatch.setattr(service, "_cuda_is_available", lambda: True)

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
        write_fake_content_list(Path(command[4]))
        return {
            "returncode": 0,
            "stdout": "OK",
            "stderr": "",
            "gpu_samples": [],
            "baseline_samples": [],
            "started_at": 2.0,
            "ended_at": 5.0,
        }

    monkeypatch.setattr(service, "_run_parse_command", fake_run)

    result = service.parse_pdf(str(source_file), task_id=12, document_id=23)

    assert calls == ["cuda", "cpu"]
    assert result.parser_provenance["effective_device"] == "cpu"
    assert result.parser_provenance["device_fallback_reason"] == "cuda_runtime_error"
